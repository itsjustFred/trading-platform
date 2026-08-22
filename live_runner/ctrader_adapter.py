from __future__ import annotations

import asyncio
import fcntl
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from live_runner.base_adapter import BaseLiveAdapter
from risk.live import size_live_signal
from shared.database import (
    find_live_signal_id_by_broker_order_id,
    get_live_signal_broker_order_id,
    get_live_signal_reconciliation_cursor,
    get_live_signal_timestamp,
    list_live_signal_ids_for_reconciliation,
    save_live_execution,
    save_live_signal,
    set_live_signal_broker_order_id,
)
from shared.logging import get_logger
from shared.models import LiveExecution, LiveSignal

logger = get_logger("live-runner")

_ASYNCIO_REACTOR_VALIDATED = False
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"


class CTraderApiError(RuntimeError):
    def __init__(self, error_code: str, description: str) -> None:
        self.error_code = str(error_code or "").strip()
        self.description = str(description or "").strip()
        message = f"{self.error_code}: {self.description}" if self.error_code else self.description
        super().__init__(message or "cTrader API error")


class CTraderAuthTerminalError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "") -> None:
        self.error_code = str(error_code or "").strip()
        super().__init__(message)


_TRUE_BOOL_TOKENS = {"1", "true", "yes", "y", "on"}
_FALSE_BOOL_TOKENS = {"0", "false", "no", "n", "off"}


def _parse_bool_env(name: str, value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    token = value.strip().lower()
    if token in _TRUE_BOOL_TOKENS:
        return True
    if token in _FALSE_BOOL_TOKENS:
        return False
    valid = ", ".join(sorted(_TRUE_BOOL_TOKENS | _FALSE_BOOL_TOKENS))
    raise ValueError(
        f"Invalid boolean value for {name}: {value!r}. Valid values are: {valid}."
    )


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


_NOT_AUTHORIZED_MARKER = "not authorized"
_CTRADER_AUTH_FAILURE_FALLBACK = "cTrader account authorization failed"


def _reason_to_text(reason: Any) -> str:
    if hasattr(reason, "getErrorMessage"):
        try:
            return str(reason.getErrorMessage())
        except Exception:
            return str(reason)
    return str(reason)


def _normalize_error_code(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "_")


def _is_already_logged_in_error(error_code: str, error_text: str) -> bool:
    code = _normalize_error_code(error_code)
    message = str(error_text or "").lower()
    if code == "ALREADY_LOGGED_IN":
        return True
    return any(
        marker in message
        for marker in (
            "already logged in",
            "already_logged_in",
            "already authorized",
        )
    )


def _is_terminal_auth_error(error_code: str, error_text: str) -> bool:
    code = _normalize_error_code(error_code)
    message = str(error_text or "").lower()
    if _NOT_AUTHORIZED_MARKER in message:
        return True
    if "invalid token" in message or "token invalid" in message:
        return True
    if "unauthorized" in message:
        return True
    return code in {
        "TRADING_ACCOUNT_IS_NOT_AUTHORIZED",
        "ACCOUNT_NOT_AUTHORIZED",
        "NOT_AUTHORIZED",
        "UNAUTHORIZED",
        "CH_ACCESS_TOKEN_INVALID",
        "INVALID_TOKEN",
        "ACCESS_TOKEN_INVALID",
    }


def _write_env_keys(path: Path, updates: dict[str, str]) -> None:
    """Persist dotenv keys atomically without logging secret material."""
    resolved = path.resolve()
    if resolved.name != path.name or not path.parent.is_dir():
        raise FileNotFoundError(
            f"Accounts env target must be a regular file in an existing directory: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            seen: set[str] = set()
            for index, line in enumerate(lines):
                key, separator, _value = line.partition("=")
                if not separator or key not in updates:
                    continue
                lines[index] = f"{key}={updates[key]}"
                seen.add(key)
            for key, value in updates.items():
                if key not in seen:
                    lines.append(f"{key}={value}")

            tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp_path.chmod(0o600)
            os.replace(tmp_path, path)
            path.chmod(0o600)
            if path.parent.exists():
                path.parent.chmod(0o700)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _write_env_key(path: Path, key: str, value: str) -> None:
    """Persist one dotenv key without logging secret material."""
    _write_env_keys(path, {key: value})


def _ensure_asyncio_twisted_reactor(loop: asyncio.AbstractEventLoop) -> None:
    global _ASYNCIO_REACTOR_VALIDATED
    try:
        from twisted.internet import asyncioreactor
        from twisted.internet.error import ReactorAlreadyInstalledError
    except ModuleNotFoundError as exc:
        raise RuntimeError("Twisted is required for cTrader OpenApiPy integration.") from exc

    if not _ASYNCIO_REACTOR_VALIDATED:
        try:
            asyncioreactor.install(loop)
        except ReactorAlreadyInstalledError:
            pass
        _ASYNCIO_REACTOR_VALIDATED = True

    from twisted.internet import reactor

    reactor_module = reactor.__class__.__module__
    if "asyncioreactor" not in reactor_module:
        raise RuntimeError(
            f"Unsupported Twisted reactor already installed: {reactor_module}. "
            "Expected asyncio reactor."
        )
    if not reactor.running:
        reactor.startRunning(installSignalHandlers=False)


class CTraderLiveAdapter(BaseLiveAdapter):
    """cTrader live adapter backed by OpenApiPy."""

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        account_id: str | int | None = None,
        demo: bool | None = None,
        account_config_id: str = "",
        accounts_env_path: Path | str | None = None,
        connect_max_attempts: int | None = None,
        connect_timeout_seconds: float | None = None,
    ):
        self.app_id = app_id or os.environ.get("CTRADER_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("CTRADER_APP_SECRET", "")
        self.access_token = access_token or os.environ.get("CTRADER_ACCESS_TOKEN", "")
        self.refresh_token = refresh_token or os.environ.get("CTRADER_REFRESH_TOKEN", "")
        self.accounts_env_path = Path(accounts_env_path) if accounts_env_path else DEFAULT_ACCOUNTS_ENV
        raw_account_id = str(account_id or account_config_id).strip()
        self.ctid_trader_account_id = int(raw_account_id) if raw_account_id else 0
        self.demo = (
            _parse_bool_env("CTRADER_DEMO", os.environ.get("CTRADER_DEMO"), default=True)
            if demo is None
            else demo
        )
        self.account_config_id = account_config_id or raw_account_id
        self.connect_max_attempts = connect_max_attempts
        self.connect_timeout_seconds = connect_timeout_seconds

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
        self._reconciliation_required = False
        self._reconciliation_retry_at = 0.0
        self._connected = False
        self._authenticated = False
        self._connect_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._symbol_name_to_id: dict[str, int] = {}
        self._order_id_to_signal_id: dict[int, str] = {}
        self._terminal_auth_failure_reason = ""
        self._terminal_auth_failure_code = ""
        self._already_logged_in_notified = False
        self._refresh_flow = False

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
        self._ProtoOAOrderDetailsReq: Any = None
        self._ProtoOAOrderDetailsRes: Any = None
        self._ProtoOAOrderListReq: Any = None
        self._ProtoOAOrderListRes: Any = None
        self._ProtoOAReconcileReq: Any = None
        self._ProtoOAReconcileRes: Any = None
        self._ProtoOARefreshTokenReq: Any = None
        self._ProtoOARefreshTokenRes: Any = None
        self._ProtoOADealStatus: Any = None
        self._ProtoOAPositionStatus: Any = None
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
                ProtoOAOrderDetailsReq,
                ProtoOAOrderDetailsRes,
                ProtoOAOrderListReq,
                ProtoOAOrderListRes,
                ProtoOAOrderErrorEvent,
                ProtoOAReconcileReq,
                ProtoOAReconcileRes,
                ProtoOARefreshTokenReq,
                ProtoOARefreshTokenRes,
                ProtoOASymbolsListReq,
                ProtoOASymbolsListRes,
                ProtoOATraderReq,
                ProtoOATraderRes,
            )
            from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
                ProtoOADealStatus,
                ProtoOAExecutionType,
                ProtoOAOrderType,
                ProtoOAPositionStatus,
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
        self._ProtoOAOrderDetailsReq = ProtoOAOrderDetailsReq
        self._ProtoOAOrderDetailsRes = ProtoOAOrderDetailsRes
        self._ProtoOAOrderListReq = ProtoOAOrderListReq
        self._ProtoOAOrderListRes = ProtoOAOrderListRes
        self._ProtoOAReconcileReq = ProtoOAReconcileReq
        self._ProtoOAReconcileRes = ProtoOAReconcileRes
        self._ProtoOARefreshTokenReq = ProtoOARefreshTokenReq
        self._ProtoOARefreshTokenRes = ProtoOARefreshTokenRes
        self._ProtoOADealStatus = ProtoOADealStatus
        self._ProtoOAPositionStatus = ProtoOAPositionStatus
        self._ProtoOAOrderType = ProtoOAOrderType
        self._ProtoOATradeSide = ProtoOATradeSide
        self._ProtoOAExecutionType = ProtoOAExecutionType
        self._ProtoOATimeInForce = ProtoOATimeInForce

        self._heartbeat_payload_type = ProtoHeartbeatEvent().payloadType
        self._execution_payload_type = ProtoOAExecutionEvent().payloadType
        self._error_payload_type = ProtoOAErrorRes().payloadType
        self._order_error_payload_type = ProtoOAOrderErrorEvent().payloadType

    def _raise_if_terminal_auth_failed(self) -> None:
        if not self._terminal_auth_failure_reason:
            return
        raise CTraderAuthTerminalError(
            self._terminal_auth_failure_reason,
            error_code=self._terminal_auth_failure_code,
        )

    def _mark_terminal_auth_failure(self, reason: str, *, error_code: str = "") -> None:
        normalized_reason = str(reason or "").strip() or _CTRADER_AUTH_FAILURE_FALLBACK
        self._terminal_auth_failure_reason = normalized_reason
        self._terminal_auth_failure_code = _normalize_error_code(error_code)
        self._authenticated = False

    async def _refresh_access_token(self, *, failed_access_token: str | None = None) -> bool:
        """Refresh cTrader OAuth tokens via Open API and persist them safely."""
        if not self.refresh_token or self._ProtoOARefreshTokenReq is None:
            logger.warning(
                "cTrader access token refresh unavailable: missing refresh token or OpenAPI type",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            return False
        refresh_token_before_lock = self.refresh_token
        async with self._refresh_lock:
            if failed_access_token is not None and self.access_token != failed_access_token:
                return True
            if self.refresh_token != refresh_token_before_lock and self.access_token:
                return True

            refresh_req = self._ProtoOARefreshTokenReq()
            refresh_req.refreshToken = self.refresh_token
            try:
                refresh_res = await self._send_request(refresh_req, timeout_seconds=15)
            except CTraderApiError as exc:
                # Use non-refresh version to avoid infinite recursion
                logger.warning(
                    "cTrader access token refresh failed",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "error_code": exc.error_code,
                            "error": exc.description,
                        }
                    },
                )
                return False
            if not isinstance(refresh_res, self._ProtoOARefreshTokenRes):
                logger.warning(
                    "Unexpected cTrader refresh response",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "response_type": type(refresh_res).__name__,
                        }
                    },
                )
                return False
            access_token = str(getattr(refresh_res, "accessToken", "") or "").strip()
            refresh_token = str(getattr(refresh_res, "refreshToken", "") or "").strip()
            if not access_token or not refresh_token:
                logger.warning(
                    "cTrader refresh response did not contain both tokens",
                    extra={"data": {"account_id": self.ctid_trader_account_id}},
                )
                return False
            try:
                _write_env_keys(
                    self.accounts_env_path,
                    {
                        "CTRADER_ACCESS_TOKEN": access_token,
                        "CTRADER_REFRESH_TOKEN": refresh_token,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "cTrader tokens refreshed but could not be persisted",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "env_path": str(self.accounts_env_path),
                            "error": str(exc),
                        }
                    },
                )
                return False

            self.access_token = access_token
            self.refresh_token = refresh_token
            os.environ["CTRADER_ACCESS_TOKEN"] = access_token
            os.environ["CTRADER_REFRESH_TOKEN"] = refresh_token
            self._terminal_auth_failure_reason = ""
            self._terminal_auth_failure_code = ""
            logger.info(
                "cTrader access token refreshed",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "expires_in": int(getattr(refresh_res, "expiresIn", 0) or 0),
                    }
                },
            )
            return True

    def _raise_terminal_auth_from_api_error(self, exc: CTraderApiError) -> None:
        error_code = _normalize_error_code(exc.error_code)
        error_text = str(exc)
        if not _is_terminal_auth_error(error_code, error_text):
            return
        reason = exc.description or error_text or _CTRADER_AUTH_FAILURE_FALLBACK
        self._mark_terminal_auth_failure(reason, error_code=error_code)
        raise CTraderAuthTerminalError(reason, error_code=error_code) from exc

    def _on_connected(self, client: Any) -> None:
        if client is not self._client:
            return
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
        if client is not self._client:
            return
        self._connected = False
        self._authenticated = False
        self._already_logged_in_notified = False
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
        if client is not self._client:
            return
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
                self._reconciliation_required = True
                logger.warning(
                    "cTrader execution queue is full; broker reconciliation required"
                )
            return

        if payload_type == self._error_payload_type:
            error_code = str(getattr(payload, "errorCode", ""))
            description = str(getattr(payload, "description", ""))
            if _is_already_logged_in_error(error_code, description):
                if not self._already_logged_in_notified:
                    logger.info(
                        "cTrader account already authenticated; ignoring duplicate auth response",
                        extra={
                            "data": {
                                "account_id": self.ctid_trader_account_id,
                                "error_code": error_code,
                            }
                        },
                    )
                    self._already_logged_in_notified = True
                return
            if _is_terminal_auth_error(error_code, description):
                self._mark_terminal_auth_failure(
                    description or str(error_code or _NOT_AUTHORIZED_MARKER),
                    error_code=error_code,
                )
                logger.warning(
                    "Account %s not authorized for token, skipping",
                    self.ctid_trader_account_id,
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "account_config_id": self.account_config_id,
                            "error_code": error_code,
                            "error": description,
                        }
                    },
                )
                return
            logger.warning(
                "cTrader API error response",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error_code": error_code,
                        "description": description,
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
            raise CTraderApiError(
                error_code=str(getattr(payload, "errorCode", "")),
                description=str(getattr(payload, "description", "")),
            )
        return payload

    async def _send_request_with_refresh_retry(
        self, request: Any, *, timeout_seconds: int = 10, _depth: int = 0
    ) -> Any:
        """Send request with automatic token refresh on invalid-token/Auth errors.

        Retries once after refreshing the access token if the error indicates
        an expired or invalid token. Prevents infinite loops via _depth counter.
        """
        if _depth > 0:
            # Already attempted refresh; don't loop infinitely
            return await self._send_request(request, timeout_seconds=timeout_seconds)

        access_token_before_send = self.access_token
        try:
            return await self._send_request(request, timeout_seconds=timeout_seconds)
        except CTraderApiError as exc:
            error_code = _normalize_error_code(exc.error_code)
            error_text = str(exc)
            if _is_terminal_auth_error(error_code, error_text) and await self._refresh_access_token(
                failed_access_token=access_token_before_send
            ):
                # Token was refreshed successfully; re-authorize the account and
                # retry once with the new token.
                if self._ProtoOAAccountAuthReq is not None and self._ProtoOAAccountAuthRes is not None:
                    account_auth_res = await self._send_account_auth_request(timeout_seconds=15)
                    if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
                        raise RuntimeError(
                            "Unexpected account auth response after refresh: "
                            f"{type(account_auth_res).__name__}"
                        )
                    self._authenticated = True
                return await self._send_request_with_refresh_retry(
                    request, timeout_seconds=timeout_seconds, _depth=_depth + 1
                )
            raise

    async def _send_account_auth_request(self, *, timeout_seconds: int = 15) -> Any:
        account_auth_req = self._ProtoOAAccountAuthReq()
        account_auth_req.ctidTraderAccountId = self.ctid_trader_account_id
        account_auth_req.accessToken = self.access_token
        return await self._send_request(account_auth_req, timeout_seconds=timeout_seconds)

    async def _authenticate(self) -> None:
        self._raise_if_terminal_auth_failed()
        if self._authenticated and self._symbol_name_to_id:
            return

        app_auth_req = self._ProtoOAApplicationAuthReq()
        app_auth_req.clientId = self.app_id
        app_auth_req.clientSecret = self.app_secret
        app_auth_res = await self._send_request(app_auth_req, timeout_seconds=15)
        if not isinstance(app_auth_res, self._ProtoOAApplicationAuthRes):
            raise RuntimeError(
                f"Unexpected application auth response: {type(app_auth_res).__name__}"
            )

        try:
            account_auth_res = await self._send_account_auth_request(timeout_seconds=15)
        except CTraderApiError as exc:
            error_code = _normalize_error_code(exc.error_code)
            error_text = str(exc)
            if _is_already_logged_in_error(error_code, error_text):
                logger.info(
                    "cTrader account already authenticated; reusing current session",
                    extra={"data": {"account_id": self.ctid_trader_account_id}},
                )
            elif _is_terminal_auth_error(error_code, error_text):
                if await self._refresh_access_token():
                    account_auth_res = await self._send_account_auth_request(timeout_seconds=15)
                    if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
                        raise RuntimeError(
                            "Unexpected account auth response after refresh: "
                            f"{type(account_auth_res).__name__}"
                        )
                else:
                    reason = exc.description or error_text or _CTRADER_AUTH_FAILURE_FALLBACK
                    self._mark_terminal_auth_failure(reason, error_code=error_code)
                    raise CTraderAuthTerminalError(reason, error_code=error_code) from exc
            else:
                raise
        else:
            if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
                raise RuntimeError(
                    f"Unexpected account auth response: {type(account_auth_res).__name__}"
                )

        symbols_req = self._ProtoOASymbolsListReq()
        symbols_req.ctidTraderAccountId = self.ctid_trader_account_id
        symbols_req.includeArchivedSymbols = False
        symbols_res = await self._send_request_with_refresh_retry(symbols_req, timeout_seconds=15)
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

        if "qty" in metadata:
            qty = _to_float(metadata.get("qty"), default=0.0)
            if qty > 0:
                volume = int(qty * 1000)
                if volume < 1000:
                    raise ValueError(
                        f"Risk-sized volume {volume} is below the broker minimum (1000); "
                        "order rejected instead of silently upsizing beyond the risk limit."
                    )
                return volume

        # Fail closed: never trade a default size, and never silently upsize a
        # risk-sized quantity to the broker minimum.
        raise ValueError(
            "Signal has no positive quantity ('qty', 'ctrader_volume' or 'volume'); "
            "refusing to submit an order with a default size."
        )

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._connected and self._authenticated and self._client is not None:
                return
            self._raise_if_terminal_auth_failed()

            self._loop = asyncio.get_running_loop()
            self._load_openapi_types()

            # Retry transient connect/auth failures (the cTrader demo endpoint
            # flaps: raw TCP is sometimes reachable, sometimes times out). Each
            # attempt builds a fresh Twisted client so a half-dead service from a
            # prior attempt can't poison the next one. Terminal auth errors
            # (bad/expired token, account not authorized) are NOT retried.
            max_attempts = (
                self.connect_max_attempts
                if self.connect_max_attempts is not None
                else int(os.environ.get("CTRADER_CONNECT_MAX_ATTEMPTS", "5"))
            )
            base_delay = float(os.environ.get("CTRADER_CONNECT_BACKOFF_S", "2.0"))
            connect_timeout = (
                self.connect_timeout_seconds
                if self.connect_timeout_seconds is not None
                else 20.0
            )
            if max_attempts < 1:
                max_attempts = 1

            for attempt in range(1, max_attempts + 1):
                try:
                    self._connected_event.clear()
                    client = self._create_client()
                    self._client = client
                    if not client.running:
                        client.startService()
                    await asyncio.wait_for(
                        self._connected_event.wait(), timeout=connect_timeout
                    )
                    await self._authenticate()
                    logger.info(
                        "cTrader account authenticated",
                        extra={
                            "data": {
                                "account_id": self.ctid_trader_account_id,
                                "symbols_loaded": len(self._symbol_name_to_id),
                                "attempt": attempt,
                            }
                        },
                    )
                    try:
                        await self.reconcile_executions()
                    except CTraderAuthTerminalError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "cTrader execution reconciliation failed; "
                            "fills may be missing until retry",
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "error": str(exc),
                                }
                            },
                        )
                    return
                except asyncio.CancelledError:
                    self._teardown_client()
                    raise
                except CTraderAuthTerminalError as exc:
                    reason = str(exc).strip() or "cTrader authorization failed"
                    self._mark_terminal_auth_failure(
                        reason,
                        error_code=getattr(exc, "error_code", ""),
                    )
                    self._teardown_client()
                    if _NOT_AUTHORIZED_MARKER in reason.lower():
                        logger.warning(
                            "Account %s not authorized for token, skipping",
                            self.ctid_trader_account_id,
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "account_config_id": self.account_config_id,
                                    "error_code": getattr(exc, "error_code", ""),
                                    "error": reason,
                                }
                            },
                        )
                    else:
                        logger.warning(
                            "cTrader auth failed for account; "
                            "skipping retries until credentials are updated",
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "account_config_id": self.account_config_id,
                                    "error_code": getattr(exc, "error_code", ""),
                                    "error": reason,
                                }
                            },
                        )
                    raise
                except Exception as exc:
                    self._authenticated = False
                    self._teardown_client()
                    error_text = str(exc).strip() or exc.__class__.__name__
                    message = error_text.lower()
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        logger.info(
                            "cTrader connect attempt %d/%d failed; retrying in %.0fs",
                            attempt,
                            max_attempts,
                            delay,
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "error": error_text,
                                    "next_attempt_in_s": delay,
                                }
                            },
                        )
                        await asyncio.sleep(delay)
                        continue
                    if "token" in message or "auth" in message:
                        logger.info(
                            "cTrader authentication unavailable; verify token/credentials, "
                            "daemon will retry",
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "error": error_text,
                                }
                            },
                        )
                    elif isinstance(exc, asyncio.TimeoutError) or "timeout" in message:
                        logger.info(
                            "cTrader connect timeout; endpoint may be unreachable, daemon will retry",
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "error": error_text,
                                }
                            },
                        )
                    else:
                        logger.info(
                            "cTrader connect/auth unavailable; daemon will retry",
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "error": error_text,
                                }
                            },
                        )
                    raise

    def _create_client(self):
        """Build a fresh OpenApiPy client with callbacks wired.

        Extracted from connect() so every retry attempt starts from a clean
        Twisted service instead of reusing a client left in a half-connected
        state after a transient failure.
        """
        host = (
            self._EndPoints.PROTOBUF_DEMO_HOST
            if self.demo
            else self._EndPoints.PROTOBUF_LIVE_HOST
        )
        client = self._Client(host, self._EndPoints.PROTOBUF_PORT, self._TcpProtocol)
        client.setConnectedCallback(self._on_connected)
        client.setDisconnectedCallback(self._on_disconnected)
        client.setMessageReceivedCallback(self._on_message)
        return client

    def _teardown_client(self) -> None:
        """Drop the current client and ask Twisted to stop its service so a later
        connect() attempt can build a fresh one. Safe to call when no client exists.
        """
        client = self._client
        self._client = None
        self._connected = False
        self._authenticated = False
        self._already_logged_in_notified = False
        if client is not None:
            try:
                stop = getattr(client, "stopService", None)
                if stop is not None:
                    stop()
            except Exception:
                pass
        try:
            self._connected_event.clear()
        except Exception:
            pass

    async def _recover_broker_order_id(
        self,
        signal_id: str,
        signal_timestamp: datetime | None,
    ) -> str | None:
        """Find exactly one account-scoped order by its stable client order id."""
        if signal_timestamp is None:
            return None
        if signal_timestamp.tzinfo is None:
            signal_timestamp = signal_timestamp.replace(tzinfo=timezone.utc)
        signal_timestamp = signal_timestamp.astimezone(timezone.utc)

        request = self._ProtoOAOrderListReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.fromTimestamp = int(
            (signal_timestamp - timedelta(minutes=15)).timestamp() * 1000
        )
        request.toTimestamp = int(
            (signal_timestamp + timedelta(minutes=15)).timestamp() * 1000
        )
        response = await self._send_request_with_refresh_retry(
            request, timeout_seconds=15
        )
        if not isinstance(response, self._ProtoOAOrderListRes):
            return None
        if bool(getattr(response, "hasMore", False)):
            logger.warning(
                "cTrader recovery order window is incomplete; refusing ambiguous match",
                extra={"data": {"signal_id": signal_id}},
            )
            return None
        matches = [
            order
            for order in getattr(response, "order", [])
            if str(getattr(order, "clientOrderId", "") or "").strip() == signal_id
            and int(getattr(order, "orderId", 0) or 0) > 0
        ]
        if len(matches) != 1:
            if len(matches) > 1:
                logger.error(
                    "Multiple cTrader orders matched one signal; refusing recovery",
                    extra={
                        "data": {"signal_id": signal_id, "match_count": len(matches)}
                    },
                )
            return None
        return str(int(matches[0].orderId))

    async def reconcile_executions(self, *, max_signals: int = 200) -> int:
        """Backfill fills for persisted signals that never received an execution record.

        Recovers fills that completed while the daemon was down or crashed between
        broker submit and stream-event persistence. Uses the broker_order_id persisted
        at submit time to fetch the order's deals via ProtoOAOrderDetailsReq. Partially
        executed signals remain eligible; deal-id dedupe prevents double-counting.
        """
        if self._client is None or not self._authenticated:
            return 0
        signal_ids: list[str] = []
        before: tuple[datetime, str] | None = None
        while True:
            if before is None:
                page = await asyncio.to_thread(
                    list_live_signal_ids_for_reconciliation,
                    self.account_config_id,
                    max_signals,
                )
            else:
                page = await asyncio.to_thread(
                    list_live_signal_ids_for_reconciliation,
                    self.account_config_id,
                    max_signals,
                    before=before,
                )
            if not page:
                break
            signal_ids.extend(page)
            if len(page) < max_signals:
                break
            next_before = await asyncio.to_thread(
                get_live_signal_reconciliation_cursor, page[-1]
            )
            if next_before is None or next_before == before:
                break
            before = next_before
        recovered = 0
        for signal_id in signal_ids:
            try:
                signal_uuid = UUID(signal_id)
            except ValueError:
                continue
            broker_order_id = await asyncio.to_thread(
                get_live_signal_broker_order_id, signal_id
            )
            if not broker_order_id:
                signal_timestamp = await asyncio.to_thread(
                    get_live_signal_timestamp, signal_id
                )
                broker_order_id = await self._recover_broker_order_id(
                    signal_id, signal_timestamp
                )
                if not broker_order_id:
                    continue
                persisted = await asyncio.to_thread(
                    set_live_signal_broker_order_id, signal_id, broker_order_id
                )
                if not persisted:
                    logger.error(
                        "Recovered cTrader order but could not persist broker order id",
                        extra={"data": {"signal_id": signal_id}},
                    )
                    continue
                self._order_id_to_signal_id[int(broker_order_id)] = signal_id
            try:
                order_id = int(broker_order_id)
            except (TypeError, ValueError):
                # Order id belongs to another broker (e.g. Alpaca UUIDs).
                continue

            details_req = self._ProtoOAOrderDetailsReq()
            details_req.ctidTraderAccountId = self.ctid_trader_account_id
            details_req.orderId = order_id
            try:
                details_res = await self._send_request_with_refresh_retry(
                    details_req,
                    timeout_seconds=15,
                )
            except CTraderApiError as exc:
                self._raise_terminal_auth_from_api_error(exc)
                # Unknown to this account (other account/broker) or not found.
                logger.info(
                    "cTrader reconciliation could not fetch order details",
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "broker_order_id": broker_order_id,
                            "error": str(exc),
                        }
                    },
                )
                continue
            if not isinstance(details_res, self._ProtoOAOrderDetailsRes):
                logger.warning(
                    "Unexpected order details response during reconciliation",
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "broker_order_id": broker_order_id,
                            "response_type": type(details_res).__name__,
                        }
                    },
                )
                continue

            for deal in getattr(details_res, "deal", []):
                deal_status = int(getattr(deal, "dealStatus", 0) or 0)
                if deal_status not in {
                    self._ProtoOADealStatus.FILLED,
                    self._ProtoOADealStatus.PARTIALLY_FILLED,
                }:
                    continue
                deal_id = int(getattr(deal, "dealId", 0) or 0)
                if deal_id <= 0:
                    continue
                filled_volume = _to_float(getattr(deal, "filledVolume", 0))
                if filled_volume <= 0:
                    filled_volume = _to_float(getattr(deal, "volume", 0))
                if filled_volume <= 0:
                    continue

                timestamp_ms = 0
                if int(getattr(deal, "executionTimestamp", 0) or 0) > 0:
                    timestamp_ms = int(deal.executionTimestamp)
                elif int(getattr(deal, "utcLastUpdateTimestamp", 0) or 0) > 0:
                    timestamp_ms = int(deal.utcLastUpdateTimestamp)

                execution = LiveExecution(
                    signal_id=signal_uuid,
                    actual_price=_to_float(getattr(deal, "executionPrice", 0)),
                    filled_qty=filled_volume,
                    commission=_to_float(getattr(deal, "commission", 0)),
                    timestamp=_to_datetime_from_ms(timestamp_ms),
                    broker_order_id=broker_order_id,
                    dedupe_key=f"ctrader:deal:{deal_id}",
                )
                if await asyncio.to_thread(save_live_execution, execution):
                    recovered += 1
                    logger.info(
                        "Reconciled missed cTrader fill",
                        extra={
                            "data": {
                                "signal_id": signal_id,
                                "broker_order_id": broker_order_id,
                                "deal_id": deal_id,
                                "filled_qty": filled_volume,
                            }
                        },
                    )
        return recovered

    async def disconnect(self) -> None:
        client = self._client
        loop = self._loop
        self._client = None
        self._loop = None
        self._authenticated = False
        self._connected = False
        self._connected_event.clear()
        self._already_logged_in_notified = False
        self._symbol_name_to_id.clear()
        self._order_id_to_signal_id.clear()
        self._execution_queue = asyncio.Queue(maxsize=5000)
        self._terminal_auth_failure_reason = ""
        self._terminal_auth_failure_code = ""

        if client is None:
            return

        is_running = bool(getattr(client, "running", False))
        if not is_running:
            return

        stop_result = client.stopService()
        if stop_result is not None and hasattr(stop_result, "asFuture") and loop is not None:
            try:
                await asyncio.wait_for(stop_result.asFuture(loop), timeout=0.1)
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out waiting for cTrader transport to stop",
                    extra={"data": {"account_id": self.ctid_trader_account_id}},
                )

    async def submit_order(self, signal: LiveSignal) -> str:
        self._raise_if_terminal_auth_failed()
        signal_id = str(signal.id)
        existing_order_id = await asyncio.to_thread(
            get_live_signal_broker_order_id, signal_id
        )
        if existing_order_id:
            try:
                self._order_id_to_signal_id[int(existing_order_id)] = signal_id
            except (TypeError, ValueError):
                pass
            return existing_order_id

        if not self._connected or not self._authenticated:
            await self.connect()

        balance, _ = await self.get_balance()
        sized_order = size_live_signal(signal, account_balance=balance)
        sized_qty = _to_float((signal.metadata or {}).get("qty"), default=0.0)
        if sized_qty <= 0:
            raise ValueError(
                "Risk engine returned non-positive quantity; order blocked "
                f"(risk_percent={sized_order.risk_percent:.4f})."
            )

        persisted_timestamp = await asyncio.to_thread(
            get_live_signal_timestamp, signal_id
        )
        if persisted_timestamp is not None:
            recovered_order_id = await self._recover_broker_order_id(
                signal_id, persisted_timestamp
            )
            if not recovered_order_id:
                raise RuntimeError(
                    "Persisted cTrader signal has no unique recoverable broker order; "
                    "refusing possible duplicate submission."
                )
            persisted = await asyncio.to_thread(
                set_live_signal_broker_order_id, signal_id, recovered_order_id
            )
            if not persisted:
                raise RuntimeError(
                    "Recovered cTrader order but failed to persist broker order id."
                )
            self._order_id_to_signal_id[int(recovered_order_id)] = signal_id
            return recovered_order_id

        canonical_signal_id, created = await asyncio.to_thread(save_live_signal, signal)
        if not created:
            existing_order_id = await asyncio.to_thread(
                get_live_signal_broker_order_id, canonical_signal_id
            )
            if existing_order_id:
                try:
                    self._order_id_to_signal_id[int(existing_order_id)] = canonical_signal_id
                except (TypeError, ValueError):
                    pass
                return existing_order_id
            canonical_timestamp = await asyncio.to_thread(
                get_live_signal_timestamp, canonical_signal_id
            )
            recovered_order_id = await self._recover_broker_order_id(
                canonical_signal_id, canonical_timestamp
            )
            if not recovered_order_id:
                raise RuntimeError(
                    "Duplicate Discord signal has no unique recoverable broker order; "
                    "refusing possible duplicate submission."
                )
            persisted = await asyncio.to_thread(
                set_live_signal_broker_order_id,
                canonical_signal_id,
                recovered_order_id,
            )
            if not persisted:
                raise RuntimeError(
                    "Recovered duplicate Discord order but failed to persist broker order id."
                )
            self._order_id_to_signal_id[int(recovered_order_id)] = canonical_signal_id
            return recovered_order_id

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

        try:
            response = await self._send_request_with_refresh_retry(request, timeout_seconds=20)
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if isinstance(response, self._ProtoOAOrderErrorEvent):
            raise RuntimeError(
                f"Order rejected ({response.errorCode}): {response.description or 'no description'}"
            )

        broker_order_id = ""
        order_id = self._extract_order_id_from_payload(response)
        if order_id is not None:
            broker_order_id = str(order_id)
            persisted = await asyncio.to_thread(
                set_live_signal_broker_order_id, canonical_signal_id, broker_order_id
            )
            if not persisted:
                raise RuntimeError("Failed to persist cTrader broker order id.")
            self._order_id_to_signal_id[order_id] = canonical_signal_id

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
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()

        self._raise_if_terminal_auth_failed()
        trader_req = self._ProtoOATraderReq()
        trader_req.ctidTraderAccountId = self.ctid_trader_account_id
        try:
            trader_res = await self._send_request_with_refresh_retry(
                trader_req,
                timeout_seconds=15,
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
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

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with signed qty for exposure limits and exit-only sizing.

        qty uses the same unit convention as signal metadata 'qty'
        (inverse of _resolve_order_volume: qty == broker volume / 1000).
        current_price/unrealized_pnl are not available without a spot
        subscription and are reported as 0.0.
        """
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()

        self._raise_if_terminal_auth_failed()
        reconcile_req = self._ProtoOAReconcileReq()
        reconcile_req.ctidTraderAccountId = self.ctid_trader_account_id
        try:
            reconcile_res = await self._send_request_with_refresh_retry(
                reconcile_req,
                timeout_seconds=15,
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if not isinstance(reconcile_res, self._ProtoOAReconcileRes):
            raise RuntimeError(
                f"Unexpected reconcile response: {type(reconcile_res).__name__}"
            )

        symbol_id_to_name = {
            symbol_id: name for name, symbol_id in self._symbol_name_to_id.items()
        }
        payload: list[dict[str, Any]] = []
        for position in getattr(reconcile_res, "position", []):
            status = int(getattr(position, "positionStatus", 0) or 0)
            if status != self._ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            trade_data = getattr(position, "tradeData", None)
            if trade_data is None:
                continue
            symbol_id = int(getattr(trade_data, "symbolId", 0) or 0)
            symbol = symbol_id_to_name.get(symbol_id, str(symbol_id))
            volume = _to_float(getattr(trade_data, "volume", 0))
            qty = volume / 1000.0
            if int(getattr(trade_data, "tradeSide", 0) or 0) == self._ProtoOATradeSide.SELL:
                qty = -qty
            payload.append(
                {
                    "symbol": str(symbol).strip().upper(),
                    "qty": qty,
                    "entry_price": _to_float(getattr(position, "price", 0)),
                    "current_price": 0.0,
                    "unrealized_pnl": 0.0,
                }
            )
        return payload

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
        if not client_order_id and order_id is not None:
            client_order_id = (
                await asyncio.to_thread(
                    find_live_signal_id_by_broker_order_id,
                    self.account_config_id,
                    str(order_id),
                )
                or ""
            )

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

        deal_id = int(getattr(deal, "dealId", 0) or 0) if deal is not None else 0
        if deal_id > 0:
            dedupe_key = f"ctrader:deal:{deal_id}"
        else:
            dedupe_key = (
                f"ctrader:order:{order_id or ''}:{timestamp_ms}:{filled_volume}"
            )

        execution = LiveExecution(
            signal_id=signal_id,
            actual_price=execution_price,
            filled_qty=filled_volume,
            commission=commission,
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=str(order_id or ""),
            dedupe_key=dedupe_key,
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

                if (
                    self._reconciliation_required
                    and time.monotonic() >= self._reconciliation_retry_at
                ):
                    try:
                        await self.reconcile_executions()
                    except CTraderAuthTerminalError:
                        raise
                    except Exception as exc:
                        self._reconciliation_retry_at = time.monotonic() + backoff_seconds
                        logger.warning(
                            "cTrader overflow reconciliation failed; queue processing continues",
                            extra={
                                "data": {
                                    "account_id": self.ctid_trader_account_id,
                                    "error": str(exc),
                                    "retry_in_seconds": backoff_seconds,
                                }
                            },
                        )
                    else:
                        self._reconciliation_required = False
                        self._reconciliation_retry_at = 0.0

                event = await asyncio.wait_for(self._execution_queue.get(), timeout=30)
                await self._persist_execution_event(event)
                backoff_seconds = 2
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except CTraderAuthTerminalError as exc:
                # Terminate so the daemon can start a fresh listener once a new
                # adapter is created after credential rotation. Parking forever
                # leaves a zombie task that blocks fill persistence silently.
                logger.warning(
                    "cTrader listener terminating after terminal auth failure",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "account_config_id": self.account_config_id,
                            "error_code": getattr(exc, "error_code", ""),
                            "error": str(exc),
                        }
                    },
                )
                return
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
