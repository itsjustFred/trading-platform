from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from live_runner.base_adapter import BaseLiveAdapter
from shared.database import save_live_execution, save_live_signal
from shared.logging import get_logger
from shared.models import LiveExecution, LiveSignal

logger = get_logger("live-runner")


def _parse_bool_env(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


class AlpacaLiveAdapter(BaseLiveAdapter):
    """Live adapter for Alpaca trading + trade update streaming."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
        account_id: str = "",
    ):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self.paper = (
            _parse_bool_env(os.environ.get("ALPACA_PAPER"), default=True)
            if paper is None
            else paper
        )
        self.account_id = account_id
        self._trading_client: Any | None = None
        self._trading_stream: Any | None = None
        self._connected = False

        if not self.api_key or not self.secret_key:
            raise ValueError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )

    async def connect(self) -> None:
        """Initialize REST and websocket clients, and verify account access."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.stream import TradingStream
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        self._trading_client = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        self._trading_stream = TradingStream(self.api_key, self.secret_key, paper=self.paper)
        self._trading_client.get_account()
        self._connected = True
        logger.info(
            "Connected Alpaca live adapter",
            extra={"data": {"account_id": self.account_id, "paper": self.paper}},
        )

    async def submit_order(self, signal: LiveSignal) -> str:
        if not self._connected or self._trading_client is None:
            await self.connect()
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc

        side = OrderSide.BUY if signal.action == "buy" else OrderSide.SELL
        qty = _to_float(signal.metadata.get("qty"), default=1.0)
        await asyncio.to_thread(save_live_signal, signal)

        request = MarketOrderRequest(
            symbol=signal.symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=str(signal.id),
        )
        order = await asyncio.to_thread(self._trading_client.submit_order, order_data=request)
        broker_order_id = str(getattr(order, "id", ""))
        logger.info(
            "Submitted Alpaca live order",
            extra={
                "data": {
                    "signal_id": str(signal.id),
                    "broker_order_id": broker_order_id,
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "qty": qty,
                }
            },
        )
        return broker_order_id

    async def get_balance(self) -> tuple[float, float]:
        if not self._connected or self._trading_client is None:
            await self.connect()
        account = await asyncio.to_thread(self._trading_client.get_account)
        balance = _to_float(getattr(account, "cash", None), default=0.0)
        equity = _to_float(getattr(account, "equity", None), default=balance)
        return balance, equity

    async def _on_trade_update(self, data: Any) -> None:
        try:
            event = str(getattr(data, "event", "")).lower()
            if event not in {"fill", "partial_fill"}:
                return

            order = getattr(data, "order", None)
            if order is None:
                logger.warning("Trade update without order payload")
                return

            client_order_id = getattr(order, "client_order_id", "")
            if not client_order_id:
                logger.warning("Trade update missing client_order_id")
                return

            try:
                signal_uuid = UUID(str(client_order_id))
            except ValueError:
                logger.warning(
                    "Trade update client_order_id is not a UUID",
                    extra={"data": {"client_order_id": str(client_order_id)}},
                )
                return

            fill_price = getattr(data, "fill_price", None)
            if fill_price is None:
                fill_price = getattr(data, "price", None)
            if fill_price is None:
                fill_price = getattr(order, "filled_avg_price", None)

            filled_qty_value = getattr(data, "filled_qty", None)
            if filled_qty_value is None:
                filled_qty_value = getattr(order, "filled_qty", None)

            commission_value = getattr(data, "commission", None)
            if commission_value is None:
                commission_value = getattr(order, "commission", None)

            actual_price = _to_float(fill_price)
            filled_qty = _to_float(filled_qty_value)
            commission = _to_float(commission_value)
            fill_ts = _to_datetime(getattr(data, "timestamp", None))
            broker_order_id = str(getattr(order, "id", ""))

            execution = LiveExecution(
                signal_id=signal_uuid,
                actual_price=actual_price,
                filled_qty=filled_qty,
                commission=commission,
                timestamp=fill_ts,
                broker_order_id=broker_order_id,
            )
            await asyncio.to_thread(save_live_execution, execution)
            logger.info(
                "Persisted Alpaca execution fill",
                extra={
                    "data": {
                        "signal_id": str(execution.signal_id),
                        "execution_id": str(execution.id),
                        "event": event,
                        "actual_price": actual_price,
                        "filled_qty": filled_qty,
                        "broker_order_id": broker_order_id,
                    }
                }
            )
        except Exception as exc:
            logger.warning(
                "Failed to process trade update",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )

    async def listen_executions(self) -> None:
        if not self._connected or self._trading_stream is None:
            await self.connect()

        backoff_seconds = 2
        while True:
            try:
                if self._trading_stream is None:
                    from alpaca.trading.stream import TradingStream

                    self._trading_stream = TradingStream(
                        self.api_key, self.secret_key, paper=self.paper
                    )
                self._trading_stream.subscribe_trade_updates(self._on_trade_update)
                await asyncio.to_thread(self._trading_stream.run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Alpaca trade stream disconnected, retrying",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "error": str(exc),
                            "retry_in_seconds": backoff_seconds,
                        }
                    },
                )
                self._trading_stream = None
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
            else:
                backoff_seconds = 2
