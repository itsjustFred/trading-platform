from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from live_runner.base_adapter import BaseLiveAdapter
from shared.database import save_live_execution, save_live_signal
from shared.logging import get_logger
from shared.models import LiveExecution, LiveSignal

logger = get_logger("live-runner")


def _parse_bool_env(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_datetime_from_ms(timestamp_ms: int | float | None) -> datetime:
    if timestamp_ms is None:
        return datetime.now(timezone.utc)
    try:
        timestamp_value = float(timestamp_ms)
        if timestamp_value <= 0:
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(timestamp_value / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _reason_to_text(reason: Any) -> str:
    if hasattr(reason, "getErrorMessage"):
        try:
            return str(reason.getErrorMessage())
        except Exception:
            return str(reason)
    return str(reason)


def _ensure_asyncio_twisted_reactor(loop: asyncio.AbstractEventLoop) -> None:
    try:
        from twisted.internet import asyncioreactor
        from twisted.internet.error import ReactorAlreadyInstalledError
    except ModuleNotFoundError as exc:
        raise RuntimeError("Twisted is required for cTrader OpenApiPy integration.") from exc

    try:
        asyncioreactor.install(loop)
    except ReactorAlreadyInstalledError:
        from twisted.internet import reactor

        reactor_module = reactor.__class__.__module__
        if "asyncioreactor" not in reactor_module:
            raise RuntimeError(
                f"Unsupported Twisted reactor already installed: {reactor_module}. "
                "Expected asyncio reactor."
            )


class CTraderLiveAdapter(BaseLiveAdapter):
    """cTrader live adapter backed by OpenApiPy."""

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        access_token: str | None = None,
        account_id: str | int | None = None,
        demo: bool | None = None,
        account_config_id: str = "",
    ):
        self.app_id = app_id or os.environ.get("CTRADER_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("CTRADER_APP_SECRET", "")
        self.access_token = access_token or os.environ.get("CTRADER_ACCESS_TOKEN", "")
        raw_account_id = str(account_id or account_config_id).strip()
        self.ctid_trader_account_id = int(raw_account_id) if raw_account_id else 0
        self.demo = (
            _parse_bool_env(os.environ.get("CTRADER_DEMO"), default=True)
            if demo is None
            else demo
        )
        self.account_config_id = account_config_id or raw_account_id

        if not self.app_id:
            raise ValueError("Missing CTRADER_APP_ID.")
        if not self.app_secret:
            raise ValueError("Missing CTRADER_APP_SECRET.")
        if not self.access_token:
            raise ValueError("Missing CTRADER_ACCESS_TOKEN.")
        if self.ctid_trader_account_id <= 0:
            raise ValueError(
                "Missing or invalid cTrader account id. Provide account_id/account_config_id."
            )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any | None = None
        self._connected_event = asyncio.Event()
        self._execution_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=5000)
        self._connected = False
        self._authenticated = False
        self._symbol_name_to_id: dict[str, int] = {}
        self._order_id_to_signal_id: dict[int, str] = {}

        self._Client: Any = None
        self._TcpProtocol: Any = None
        self._EndPoints: Any = None
        self._Protobuf: Any = None
        self._ProtoHeartbeatEvent: Any = None
        self._ProtoOAApplicationAuthReq: Any = None
        self._ProtoOAApplicationAuthRes: Any = None
        self._ProtoOAAccountAuthReq: Any = None
        self._ProtoOAAccountAuthRes: Any = None
        self._ProtoOASymbolsListReq: Any = None
        self._ProtoOASymbolsListRes: Any = None
        self._ProtoOANewOrderReq: Any = None
        self._ProtoOAExecutionEvent: Any = None
        self._ProtoOAOrderErrorEvent: Any = None
        self._ProtoOAErrorRes: Any = None
        self._ProtoOATraderReq: Any = None
        self._ProtoOATraderRes: Any = None
        self._ProtoOAOrderType: Any = None
        self._ProtoOATradeSide: Any = None
        self._ProtoOAExecutionType: Any = None
        self._ProtoOATimeInForce: Any = None

        self._heartbeat_payload_type: int | None = None
        self._execution_payload_type: int | None = None
        self._error_payload_type: int | None = None
        self._order_error_payload_type: int | None = None

    def _load_openapi_types(self) -> None:
        if self._Client is not None:
            return
        assert self._loop is not None
        _ensure_asyncio_twisted_reactor(self._loop)
        try:
            from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
            from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAAccountAuthReq,
                ProtoOAAccountAuthRes,
                ProtoOAApplicationAuthReq,
                ProtoOAApplicationAuthRes,
                ProtoOAErrorRes,
                ProtoOAExecutionEvent,
                ProtoOANewOrderReq,
                ProtoOAOrderErrorEvent,
                ProtoOASymbolsListReq,
                ProtoOASymbolsListRes,
                ProtoOATraderReq,
                ProtoOATraderRes,
            )
            from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
                ProtoOAExecutionType,
                ProtoOAOrderType,
                ProtoOATimeInForce,
                ProtoOATradeSide,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ctrader-open-api package is required for CTraderLiveAdapter."
            ) from exc

        self._Client = Client
        self._TcpProtocol = TcpProtocol
        self._EndPoints = EndPoints
        self._Protobuf = Protobuf
        self._ProtoHeartbeatEvent = ProtoHeartbeatEvent
        self._ProtoOAApplicationAuthReq = ProtoOAApplicationAuthReq
        self._ProtoOAApplicationAuthRes = ProtoOAApplicationAuthRes
        self._ProtoOAAccountAuthReq = ProtoOAAccountAuthReq
        self._ProtoOAAccountAuthRes = ProtoOAAccountAuthRes
        self._ProtoOASymbolsListReq = ProtoOASymbolsListReq
        self._ProtoOASymbolsListRes = ProtoOASymbolsListRes
        self._ProtoOANewOrderReq = ProtoOANewOrderReq
        self._ProtoOAExecutionEvent = ProtoOAExecutionEvent
        self._ProtoOAOrderErrorEvent = ProtoOAOrderErrorEvent
        self._ProtoOAErrorRes = ProtoOAErrorRes
        self._ProtoOATraderReq = ProtoOATraderReq
        self._ProtoOATraderRes = ProtoOATraderRes
        self._ProtoOAOrderType = ProtoOAOrderType
        self._ProtoOATradeSide = ProtoOATradeSide
        self._ProtoOAExecutionType = ProtoOAExecutionType
        self._ProtoOATimeInForce = ProtoOATimeInForce

        self._heartbeat_payload_type = ProtoHeartbeatEvent().payloadType
        self._execution_payload_type = ProtoOAExecutionEvent().payloadType
        self._error_payload_type = ProtoOAErrorRes().payloadType
        self._order_error_payload_type = ProtoOAOrderErrorEvent().payloadType

    def _on_connected(self, client: Any) -> None:
        self._connected = True
        self._connected_event.set()
        logger.info(
            "cTrader transport connected",
            extra={
                "data": {
                    "account_id": self.ctid_trader_account_id,
                    "account_config_id": self.account_config_id,
                    "demo": self.demo,
                }
            },
        )

    def _on_disconnected(self, client: Any, reason: Any) -> None:
        self._connected = False
        self._authenticated = False
        self._connected_event.clear()
        logger.warning(
            "cTrader transport disconnected",
            extra={
                "data": {
                    "account_id": self.ctid_trader_account_id,
                    "reason": _reason_to_text(reason),
                }
            },
        )

    def _on_message(self, client: Any, message: Any) -> None:
        payload_type = int(getattr(message, "payloadType", -1))
        if payload_type == self._heartbeat_payload_type:
            return
        try:
            payload = self._Protobuf.extract(message)
        except Exception as exc:
            logger.warning("Could not decode cTrader payload", extra={"data": {"error": str(exc)}})
            return

        if payload_type == self._execution_payload_type:
            try:
                self._execution_queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("cTrader execution queue is full, dropping execution event")
            return

        if payload_type == self._error_payload_type:
            logger.warning(
                "cTrader API error response",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error_code": getattr(payload, "errorCode", ""),
                        "description": getattr(payload, "description", ""),
                    }
                },
            )
            return

        if payload_type == self._order_error_payload_type:
            logger.warning(
                "cTrader order error event",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error_code": getattr(payload, "errorCode", ""),
                        "description": getattr(payload, "description", ""),
                        "order_id": str(getattr(payload, "orderId", "")),
                    }
                },
            )

    async def _send_request(self, request: Any, *, timeout_seconds: int = 10) -> Any:
        if self._client is None or self._loop is None:
            raise RuntimeError("cTrader client is not initialized.")
        deferred = self._client.send(request, responseTimeoutInSeconds=timeout_seconds)
        response_message = await deferred.asFuture(self._loop)
        payload = self._Protobuf.extract(response_message)
        if isinstance(payload, self._ProtoOAErrorRes):
            raise RuntimeError(f"{payload.errorCode}: {payload.description}")
        return payload

    async def _authenticate(self) -> None:
        app_auth_req = self._ProtoOAApplicationAuthReq()
        app_auth_req.clientId = self.app_id
        app_auth_req.clientSecret = self.app_secret
        app_auth_res = await self._send_request(app_auth_req, timeout_seconds=15)
        if not isinstance(app_auth_res, self._ProtoOAApplicationAuthRes):
            raise RuntimeError(
                f"Unexpected application auth response: {type(app_auth_res).__name__}"
            )

        account_auth_req = self._ProtoOAAccountAuthReq()
        account_auth_req.ctidTraderAccountId = self.ctid_trader_account_id
        account_auth_req.accessToken = self.access_token
        account_auth_res = await self._send_request(account_auth_req, timeout_seconds=15)
        if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
            raise RuntimeError(
                f"Unexpected account auth response: {type(account_auth_res).__name__}"
            )

        symbols_req = self._ProtoOASymbolsListReq()
        symbols_req.ctidTraderAccountId = self.ctid_trader_account_id
        symbols_req.includeArchivedSymbols = False
        symbols_res = await self._send_request(symbols_req, timeout_seconds=15)
        if not isinstance(symbols_res, self._ProtoOASymbolsListRes):
            raise RuntimeError(f"Unexpected symbols response: {type(symbols_res).__name__}")

        symbol_name_to_id: dict[str, int] = {}
        for symbol in symbols_res.symbol:
            symbol_name_to_id[str(symbol.symbolName).upper()] = int(symbol.symbolId)
        self._symbol_name_to_id = symbol_name_to_id
        self._authenticated = True

    def _extract_order_id_from_payload(self, payload: Any) -> int | None:
        if isinstance(payload, self._ProtoOAExecutionEvent):
            if payload.HasField("order") and payload.order.orderId > 0:
                return int(payload.order.orderId)
            if payload.HasField("deal") and payload.deal.orderId > 0:
                return int(payload.deal.orderId)
        if hasattr(payload, "orderId") and int(getattr(payload, "orderId", 0)) > 0:
            return int(payload.orderId)
        return None

    def _resolve_order_volume(self, signal: LiveSignal) -> int:
        metadata = signal.metadata or {}
        if "ctrader_volume" in metadata:
            try:
                explicit = int(float(metadata["ctrader_volume"]))
            except (TypeError, ValueError):
                raise ValueError("Invalid 'ctrader_volume' in signal metadata.")
            if explicit <= 0:
                raise ValueError("'ctrader_volume' must be > 0.")
            return explicit

        if "volume" in metadata:
            try:
                explicit = int(float(metadata["volume"]))
            except (TypeError, ValueError):
                raise ValueError("Invalid 'volume' in signal metadata.")
            if explicit <= 0:
                raise ValueError("'volume' must be > 0.")
            return explicit

        qty = _to_float(metadata.get("qty"), default=1.0)
        return max(int(qty * 1000), 1000)

    async def connect(self) -> None:
        if self._connected and self._authenticated and self._client is not None:
            return

        self._loop = asyncio.get_running_loop()
        self._load_openapi_types()

        if self._client is None:
            host = (
                self._EndPoints.PROTOBUF_DEMO_HOST
                if self.demo
                else self._EndPoints.PROTOBUF_LIVE_HOST
            )
            self._client = self._Client(host, self._EndPoints.PROTOBUF_PORT, self._TcpProtocol)
            self._client.setConnectedCallback(self._on_connected)
            self._client.setDisconnectedCallback(self._on_disconnected)
            self._client.setMessageReceivedCallback(self._on_message)
            logger.info(
                "cTrader client initialized (OpenApiPy TcpProtocol handles heartbeat keepalive)",
                extra={"data": {"host": host, "port": self._EndPoints.PROTOBUF_PORT}},
            )

        if not self._client.running:
            self._client.startService()

        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=20)
            await self._authenticate()
            logger.info(
                "cTrader account authenticated",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "symbols_loaded": len(self._symbol_name_to_id),
                    }
                },
            )
        except Exception as exc:
            self._authenticated = False
            message = str(exc).lower()
            if "token" in message or "auth" in message:
                logger.error(
                    "cTrader authentication failed (token expired or invalid credentials)",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "error": str(exc),
                        }
                    },
                )
            else:
                logger.error(
                    "cTrader connect/auth failed",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "error": str(exc),
                        }
                    },
                )
            raise

    async def submit_order(self, signal: LiveSignal) -> str:
        if not self._connected or not self._authenticated:
            await self.connect()

        await asyncio.to_thread(save_live_signal, signal)
        symbol_id = self._symbol_name_to_id.get(signal.symbol.upper())
        if symbol_id is None:
            await self._authenticate()
            symbol_id = self._symbol_name_to_id.get(signal.symbol.upper())
        if symbol_id is None:
            raise ValueError(f"Unknown cTrader symbol: {signal.symbol}")

        if signal.action not in {"buy", "sell"}:
            raise ValueError(f"Unsupported signal action: {signal.action}")

        request = self._ProtoOANewOrderReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.symbolId = int(symbol_id)
        request.orderType = self._ProtoOAOrderType.MARKET
        request.tradeSide = (
            self._ProtoOATradeSide.BUY if signal.action == "buy" else self._ProtoOATradeSide.SELL
        )
        request.volume = self._resolve_order_volume(signal)
        request.timeInForce = self._ProtoOATimeInForce.IMMEDIATE_OR_CANCEL
        request.clientOrderId = str(signal.id)

        response = await self._send_request(request, timeout_seconds=20)
        if isinstance(response, self._ProtoOAOrderErrorEvent):
            raise RuntimeError(
                f"Order rejected ({response.errorCode}): {response.description or 'no description'}"
            )

        broker_order_id = ""
        order_id = self._extract_order_id_from_payload(response)
        if order_id is not None:
            broker_order_id = str(order_id)
            self._order_id_to_signal_id[order_id] = str(signal.id)

        logger.info(
            "Submitted cTrader live order",
            extra={
                "data": {
                    "signal_id": str(signal.id),
                    "account_id": self.ctid_trader_account_id,
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "symbol_id": symbol_id,
                    "volume": request.volume,
                    "broker_order_id": broker_order_id,
                }
            },
        )
        return broker_order_id

    async def get_balance(self) -> tuple[float, float]:
        if not self._connected or not self._authenticated:
            await self.connect()

        trader_req = self._ProtoOATraderReq()
        trader_req.ctidTraderAccountId = self.ctid_trader_account_id
        trader_res = await self._send_request(trader_req, timeout_seconds=15)
        if not isinstance(trader_res, self._ProtoOATraderRes):
            raise RuntimeError(f"Unexpected trader response: {type(trader_res).__name__}")

        trader = trader_res.trader
        money_digits = int(getattr(trader, "moneyDigits", 2) or 2)
        scale = 10**money_digits if 0 <= money_digits <= 12 else 100
        balance = _to_float(getattr(trader, "balance", 0)) / scale
        equity = balance
        if hasattr(trader, "equity"):
            equity = _to_float(getattr(trader, "equity", balance)) / scale
        return balance, equity

    async def _persist_execution_event(self, event: Any) -> None:
        execution_type = int(event.executionType)
        if execution_type not in {
            self._ProtoOAExecutionType.ORDER_FILLED,
            self._ProtoOAExecutionType.ORDER_PARTIAL_FILL,
        }:
            return

        order = event.order if event.HasField("order") else None
        deal = event.deal if event.HasField("deal") else None

        order_id: int | None = None
        client_order_id = ""
        if order is not None:
            if order.orderId > 0:
                order_id = int(order.orderId)
            if order.clientOrderId:
                client_order_id = str(order.clientOrderId)
        if order_id is None and deal is not None and deal.orderId > 0:
            order_id = int(deal.orderId)
        if not client_order_id and order_id is not None:
            client_order_id = self._order_id_to_signal_id.get(order_id, "")

        if not client_order_id:
            logger.warning(
                "cTrader fill missing clientOrderId mapping",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "order_id": str(order_id or ""),
                        "execution_type": execution_type,
                    }
                },
            )
            return

        try:
            signal_id = UUID(client_order_id)
        except ValueError:
            logger.warning(
                "cTrader fill has non-UUID clientOrderId",
                extra={
                    "data": {
                        "client_order_id": client_order_id,
                        "order_id": str(order_id or ""),
                    }
                },
            )
            return

        execution_price = 0.0
        if deal is not None and deal.executionPrice:
            execution_price = _to_float(deal.executionPrice)
        elif order is not None and order.executionPrice:
            execution_price = _to_float(order.executionPrice)

        filled_volume = 0.0
        if deal is not None:
            if deal.filledVolume > 0:
                filled_volume = _to_float(deal.filledVolume)
            elif deal.volume > 0:
                filled_volume = _to_float(deal.volume)
        elif order is not None and order.executedVolume > 0:
            filled_volume = _to_float(order.executedVolume)

        commission = _to_float(deal.commission) if deal is not None else 0.0
        timestamp_ms = 0
        if deal is not None and deal.executionTimestamp > 0:
            timestamp_ms = int(deal.executionTimestamp)
        elif deal is not None and deal.utcLastUpdateTimestamp > 0:
            timestamp_ms = int(deal.utcLastUpdateTimestamp)
        elif order is not None and order.utcLastUpdateTimestamp > 0:
            timestamp_ms = int(order.utcLastUpdateTimestamp)

        execution = LiveExecution(
            signal_id=signal_id,
            actual_price=execution_price,
            filled_qty=filled_volume,
            commission=commission,
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=str(order_id or ""),
        )
        await asyncio.to_thread(save_live_execution, execution)
        logger.info(
            "Persisted cTrader execution fill",
            extra={
                "data": {
                    "signal_id": str(execution.signal_id),
                    "execution_id": str(execution.id),
                    "broker_order_id": execution.broker_order_id,
                    "execution_type": execution_type,
                    "actual_price": execution.actual_price,
                    "filled_qty": execution.filled_qty,
                }
            },
        )

    async def listen_executions(self) -> None:
        backoff_seconds = 2
        while True:
            try:
                if not self._connected or not self._authenticated:
                    await self.connect()

                event = await asyncio.wait_for(self._execution_queue.get(), timeout=30)
                await self._persist_execution_event(event)
                backoff_seconds = 2
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "cTrader listener loop error, retrying",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "error": str(exc),
                            "retry_in_seconds": backoff_seconds,
                        }
                    },
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
