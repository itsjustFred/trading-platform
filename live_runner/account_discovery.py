from __future__ import annotations

import math
import re
import socket
import ssl
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Discovery uses raw TLS sockets and must not install or mutate a process-wide
# Twisted reactor at import time. The live adapter bootstraps its asyncio reactor
# lazily against the running event loop before importing OpenApiPy's Client.
from ctrader_open_api import Protobuf
from ctrader_open_api.messages import OpenApiMessages_pb2 as _OpenApiMessages_pb2
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent, ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOATraderReq,
    ProtoOATraderRes,
)
from google.protobuf.json_format import MessageToDict

from live_runner.credential_env import load_accounts_env_values
from shared.logging import get_logger
from shared.models import LiveAccount

logger = get_logger("live-account-discovery")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"

_ALPACA_ACC_RE = re.compile(r"^ALPACA_API_KEY_ACC(?P<index>\d+)$")
_PROTO_OA_CTID_TRADER_ACCOUNT_REQ: Any = getattr(
    _OpenApiMessages_pb2, "ProtoOACtidTraderAccountReq", None
)
_PROTO_OA_CTID_TRADER_ACCOUNT_RES: Any = getattr(
    _OpenApiMessages_pb2, "ProtoOACtidTraderAccountRes", None
)
_CTRADER_ACCOUNT_NAME_FIELDS = (
    "name",
    "accountName",
    "displayName",
    "nickname",
    "nickName",
    "traderName",
    "label",
)
_CTRADER_BROKER_NAME_FIELDS = (
    "brokerName",
    "broker",
    "companyName",
    "firmName",
    "serverName",
)


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _parse_bool_env(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _alpaca_paper_from_endpoint(endpoint: str) -> bool:
    normalized = endpoint.strip().lower()
    if not normalized:
        return True
    return "paper" in normalized


def _ctrader_host_matches_environment(*, host: str, is_live: bool) -> bool:
    normalized_host = host.strip().lower()
    if "demo" in normalized_host:
        return not is_live
    if "live" in normalized_host:
        return is_live
    return True


def _is_expected_ctrader_host_route_error(error_text: str) -> bool:
    normalized = error_text.upper()
    return "CANT_ROUTE_REQUEST" in normalized and "NO ENVIRONMENT CONNECTION" in normalized


def _is_ctrader_account_not_authorized_error(error_text: str) -> bool:
    normalized = error_text.upper()
    return (
        "TRADING_ACCOUNT_IS_NOT_AUTHORIZED" in normalized
        or "ACCOUNT_NOT_AUTHORIZED" in normalized
        or "NOT AUTHORIZED" in normalized
    )


def _iter_alpaca_credentials(env: dict[str, str]) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    indexes = sorted(
        {
            int(match.group("index"))
            for key in env
            if (match := _ALPACA_ACC_RE.match(key))
        }
    )

    for idx in indexes:
        api_key = str(env.get(f"ALPACA_API_KEY_ACC{idx}", "")).strip()
        secret_key = str(env.get(f"ALPACA_SECRET_KEY_ACC{idx}", "")).strip()
        endpoint = str(env.get(f"ALPACA_ENDPOINT_ACC{idx}", "")).strip()
        if not api_key or not secret_key:
            continue
        indexed.append(
            {
                "key": f"ACC{idx}",
                "api_key": api_key,
                "secret_key": secret_key,
                "paper": _alpaca_paper_from_endpoint(endpoint),
            }
        )

    if indexed:
        return indexed

    api_key = str(env.get("ALPACA_API_KEY", "")).strip()
    secret_key = str(env.get("ALPACA_SECRET_KEY", "")).strip()
    if not api_key or not secret_key:
        return []
    return [
        {
            "key": "DEFAULT",
            "api_key": api_key,
            "secret_key": secret_key,
            "paper": _parse_bool_env(env.get("ALPACA_PAPER"), default=True),
        }
    ]


def discover_alpaca_accounts(api_key: str, secret_key: str, paper: bool) -> list[LiveAccount]:
    """Discover Alpaca account details for one credential set."""
    if not api_key.strip() or not secret_key.strip():
        return []

    try:
        from alpaca.trading.client import TradingClient
    except ModuleNotFoundError as exc:
        raise RuntimeError("alpaca-py is required for Alpaca account discovery.") from exc

    client = TradingClient(api_key.strip(), secret_key.strip(), paper=paper)
    account = client.get_account()

    account_id = str(getattr(account, "id", "") or "").strip()
    account_number = str(getattr(account, "account_number", "") or "").strip()
    if not account_id:
        account_id = account_number
    if not account_id:
        raise RuntimeError("Alpaca account discovery returned no account id.")

    status = str(getattr(account, "status", "") or "").strip()
    equity = _safe_float(getattr(account, "equity", None), default=math.nan)
    cash = _safe_float(getattr(account, "cash", None), default=0.0)
    balance = cash if math.isnan(equity) else equity

    display_name = f"Alpaca {account_number}" if account_number else "Alpaca Account"
    if status:
        display_name = f"{display_name} ({status})"

    return [
        LiveAccount(
            id=account_id,
            name=display_name,
            broker="alpaca",
            balance=balance,
            is_demo=bool(paper),
            broker_login=account_number,
        )
    ]


def _trader_balance(trader: Any) -> float:
    money_digits = int(getattr(trader, "moneyDigits", 2) or 2)
    scale = 10**money_digits if 0 <= money_digits <= 12 else 100
    return _safe_float(getattr(trader, "balance", 0.0), default=0.0) / scale


def _ctrader_message_name_sources(message: Any) -> list[Any]:
    if message is None:
        return []

    sources: list[Any] = [message]
    for nested_field in ("ctidTraderAccount", "account", "trader"):
        nested = getattr(message, nested_field, None)
        if nested is None:
            continue
        if hasattr(nested, "DESCRIPTOR"):
            sources.append(nested)
            continue
        if isinstance(nested, (str, bytes, bytearray)):
            continue
        try:
            for item in nested:
                if item is not None:
                    sources.append(item)
        except TypeError:
            sources.append(nested)
    return sources


def _ctrader_ignored_labels(ignored_values: set[str] | None) -> set[str]:
    return {value.strip() for value in (ignored_values or set()) if value and value.strip()}


def _ctrader_label_candidate(source: Any, field_name: str, ignored: set[str]) -> str:
    raw_value = getattr(source, field_name, "")
    candidate = str(raw_value or "").strip()
    if not candidate or candidate in ignored or candidate.isdigit():
        return ""
    return candidate


def _ctrader_extract_account_label(
    *sources: Any, ignored_values: set[str] | None = None
) -> str:
    ignored = _ctrader_ignored_labels(ignored_values)
    for source in sources:
        if source is None:
            continue
        for field_name in _CTRADER_ACCOUNT_NAME_FIELDS:
            candidate = _ctrader_label_candidate(source, field_name, ignored)
            if candidate:
                return candidate
    return ""


def _ctrader_extract_broker_name(*sources: Any) -> str:
    for source in sources:
        if source is None:
            continue
        for field_name in _CTRADER_BROKER_NAME_FIELDS:
            raw_value = getattr(source, field_name, "")
            candidate = str(raw_value or "").strip()
            if not candidate or candidate.isdigit():
                continue
            return candidate
    return ""


def _ctrader_extract_login(*sources: Any) -> str:
    for source in sources:
        if source is None:
            continue
        for field_name in ("traderLogin", "login", "traderAccountId"):
            raw_value = getattr(source, field_name, None)
            if raw_value is None:
                continue
            candidate = str(raw_value).strip()
            if not candidate:
                continue
            if candidate.isdigit() and int(candidate) <= 0:
                continue
            return candidate
    return ""


def _ctrader_account_name(
    *,
    account_id: int | str,
    account_label: str,
) -> str:
    """Resolve a display name for a cTrader account.

    Priority:
    1) Use a human-readable account label discovered from API fields.
    2) Fallback to the exact format "cTrader {accountId}" when no label is available.

    Do NOT attempt any broker-specific fallbacks (e.g., "Fusion Markets Demo #N").
    """
    resolved_name = account_label.strip()
    if resolved_name:
        return resolved_name

    normalized_account_id = str(account_id).strip()
    # Explicit exact fallback format required by task
    return f"cTrader {normalized_account_id}" if normalized_account_id else "cTrader"


def _message_to_debug_fields(message: Any) -> dict[str, Any]:
    if message is None:
        return {}
    if hasattr(message, "DESCRIPTOR"):
        try:
            payload = MessageToDict(
                message,
                preserving_proto_field_name=True,
                use_integers_for_enums=True,
                including_default_value_fields=True,
            )
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    if not hasattr(message, "ListFields"):
        return {}
    payload: dict[str, Any] = {}
    for descriptor, value in message.ListFields():
        field_name = str(descriptor.name)
        if descriptor.label == descriptor.LABEL_REPEATED:
            if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, bytearray)):
                payload[field_name] = [str(item) for item in value]
            else:
                payload[field_name] = str(value)
            continue
        payload[field_name] = str(value)
    return payload


def _ctrader_recv_exact(sock_obj: ssl.SSLSocket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock_obj.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("cTrader socket closed unexpectedly.")
        buf += chunk
    return buf


def _ctrader_send_message(sock_obj: ssl.SSLSocket, message: Any, client_msg_id: str) -> None:
    envelope = ProtoMessage(
        payloadType=int(message.payloadType),
        payload=message.SerializeToString(),
        clientMsgId=client_msg_id,
    )
    raw = envelope.SerializeToString()
    sock_obj.sendall(struct.pack(">I", len(raw)) + raw)


def _ctrader_wait_response(
    sock_obj: ssl.SSLSocket, client_msg_id: str, timeout_seconds: float
) -> Any:
    deadline = time.time() + timeout_seconds
    heartbeat_payload_type = ProtoHeartbeatEvent().payloadType

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError()
        sock_obj.settimeout(remaining)
        frame_size = struct.unpack(">I", _ctrader_recv_exact(sock_obj, 4))[0]
        frame_payload = _ctrader_recv_exact(sock_obj, frame_size)
        envelope = ProtoMessage()
        envelope.ParseFromString(frame_payload)

        if envelope.payloadType == heartbeat_payload_type:
            heartbeat = ProtoHeartbeatEvent()
            _ctrader_send_message(sock_obj, heartbeat, "")
            continue

        payload = Protobuf.extract(envelope)
        if envelope.clientMsgId != client_msg_id:
            continue
        if isinstance(payload, ProtoOAErrorRes):
            raise RuntimeError(f"{payload.errorCode}: {payload.description}")
        return payload


def _authenticate_ctrader_application(
    sock: ssl.SSLSocket,
    *,
    app_id: str,
    app_secret: str,
) -> None:
    app_auth_req = ProtoOAApplicationAuthReq()
    app_auth_req.clientId = app_id
    app_auth_req.clientSecret = app_secret
    app_auth_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(sock, app_auth_req, app_auth_client_msg_id)
    app_auth_res = _ctrader_wait_response(sock, app_auth_client_msg_id, 20)
    if not isinstance(app_auth_res, ProtoOAApplicationAuthRes):
        raise RuntimeError(f"Unexpected application auth response: {type(app_auth_res).__name__}")


def _request_ctrader_account_list(
    sock: ssl.SSLSocket,
    *,
    access_token: str,
) -> Any:
    list_req = ProtoOAGetAccountListByAccessTokenReq()
    list_req.accessToken = access_token
    list_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(sock, list_req, list_client_msg_id)
    list_res = _ctrader_wait_response(sock, list_client_msg_id, 20)
    if not isinstance(list_res, ProtoOAGetAccountListByAccessTokenRes):
        raise RuntimeError(f"Unexpected account list response: {type(list_res).__name__}")
    return list_res


@dataclass
class _CtraderAccountContext:
    host: str
    sock: ssl.SSLSocket
    access_token: str
    balance: float = 0.0
    broker_name: str = ""
    trader_login: str = ""
    account_name_hint: str = ""
    ignored_name_values: set[str] = field(default_factory=set)


def _ctrader_account_context(
    host: str,
    sock: ssl.SSLSocket,
    access_token: str,
    account_id: int,
    account_item: Any,
) -> _CtraderAccountContext:
    broker_name = _ctrader_extract_broker_name(*_ctrader_message_name_sources(account_item))
    trader_login = _ctrader_extract_login(*_ctrader_message_name_sources(account_item))
    ignored_name_values = {str(account_id)}
    if trader_login:
        ignored_name_values.add(trader_login)
    account_name_hint = _ctrader_extract_account_label(
        *_ctrader_message_name_sources(account_item),
        ignored_values=ignored_name_values,
    )
    return _CtraderAccountContext(
        host=host,
        sock=sock,
        access_token=access_token,
        broker_name=broker_name,
        trader_login=trader_login,
        account_name_hint=account_name_hint,
        ignored_name_values=ignored_name_values,
    )


def _ctrader_resolved_account_id(account_item: Any) -> int:
    return int(
        getattr(account_item, "ctidTraderAccountId", 0)
        or getattr(account_item, "traderAccountId", 0)
        or 0
    )


def _log_ctrader_account_missing_ctid(host: str, account_item: Any) -> None:
    logger.debug(
        "Skipping cTrader account without ctidTraderAccountId",
        extra={
            "data": {
                "host": host,
                "account_fields": _message_to_debug_fields(account_item),
            }
        },
    )


def _log_ctrader_host_environment_mismatch(host: str, account_id: int, is_demo: bool) -> None:
    logger.debug(
        "Skipping cTrader account on host with mismatched environment",
        extra={
            "data": {
                "account_id": str(account_id),
                "host": host,
                "is_demo": is_demo,
            }
        },
    )


def _apply_ctrader_account_detail(
    ctx: _CtraderAccountContext,
    account_id: int,
    account_detail_res: Any,
) -> None:
    logger.debug(
        "Resolved cTrader account detail payload",
        extra={
            "data": {
                "host": ctx.host,
                "ctid": str(account_id),
                "account_detail_fields": _message_to_debug_fields(account_detail_res),
            }
        },
    )
    ctx.broker_name = ctx.broker_name or _ctrader_extract_broker_name(
        *_ctrader_message_name_sources(account_detail_res)
    )
    if not ctx.account_name_hint:
        ctx.account_name_hint = _ctrader_extract_account_label(
            *_ctrader_message_name_sources(account_detail_res),
            ignored_values=ctx.ignored_name_values,
        )
    detail_login = _ctrader_extract_login(*_ctrader_message_name_sources(account_detail_res))
    if detail_login:
        ctx.trader_login = detail_login
        ctx.ignored_name_values.add(detail_login)


class _CtraderStreamError(RuntimeError):
    pass


def _request_ctrader_account_detail(ctx: _CtraderAccountContext, account_id: int) -> None:
    try:
        if _PROTO_OA_CTID_TRADER_ACCOUNT_REQ is None:
            return
        account_detail_req = _PROTO_OA_CTID_TRADER_ACCOUNT_REQ()
        if hasattr(account_detail_req, "ctidTraderAccountId"):
            account_detail_req.ctidTraderAccountId = account_id
        if hasattr(account_detail_req, "accessToken"):
            account_detail_req.accessToken = ctx.access_token
        account_detail_client_msg_id = str(uuid.uuid4())
        _ctrader_send_message(ctx.sock, account_detail_req, account_detail_client_msg_id)
        account_detail_res = _ctrader_wait_response(ctx.sock, account_detail_client_msg_id, 20)
        if _PROTO_OA_CTID_TRADER_ACCOUNT_RES is None or isinstance(
            account_detail_res, _PROTO_OA_CTID_TRADER_ACCOUNT_RES
        ):
            _apply_ctrader_account_detail(ctx, account_id, account_detail_res)
    except (TimeoutError, ConnectionError, OSError) as exc:
        logger.warning(
            "cTrader account detail request desynchronized the socket",
            extra={
                "data": {
                    "account_id": str(account_id),
                    "host": ctx.host,
                    "error": repr(exc),
                }
            },
        )
        raise _CtraderStreamError(str(exc)) from exc
    except Exception as exc:
        logger.debug(
            "Optional cTrader account detail request failed",
            extra={
                "data": {
                    "account_id": str(account_id),
                    "host": ctx.host,
                    "error": repr(exc),
                }
            },
        )


def _load_ctrader_trader_details(ctx: _CtraderAccountContext, account_id: int) -> None:
    account_auth_req = ProtoOAAccountAuthReq()
    account_auth_req.ctidTraderAccountId = account_id
    account_auth_req.accessToken = ctx.access_token
    account_auth_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(ctx.sock, account_auth_req, account_auth_client_msg_id)
    account_auth_res = _ctrader_wait_response(ctx.sock, account_auth_client_msg_id, 20)
    if not isinstance(account_auth_res, ProtoOAAccountAuthRes):
        raise RuntimeError(f"Unexpected account auth response: {type(account_auth_res).__name__}")

    trader_req = ProtoOATraderReq()
    trader_req.ctidTraderAccountId = account_id
    trader_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(ctx.sock, trader_req, trader_client_msg_id)
    trader_res = _ctrader_wait_response(ctx.sock, trader_client_msg_id, 20)
    if not isinstance(trader_res, ProtoOATraderRes):
        return
    logger.debug(
        "Resolved cTrader trader payload",
        extra={
            "data": {
                "host": ctx.host,
                "ctid": str(account_id),
                "trader_fields": _message_to_debug_fields(trader_res.trader),
            }
        },
    )
    trader = trader_res.trader
    ctx.balance = _trader_balance(trader)
    ctx.broker_name = ctx.broker_name or _ctrader_extract_broker_name(
        *_ctrader_message_name_sources(trader_res),
        *_ctrader_message_name_sources(trader),
    )
    resolved_login = _ctrader_extract_login(
        *_ctrader_message_name_sources(trader_res),
        *_ctrader_message_name_sources(trader),
    )
    if resolved_login:
        ctx.trader_login = resolved_login
        ctx.ignored_name_values.add(resolved_login)
    ctx.account_name_hint = ctx.account_name_hint or _ctrader_extract_account_label(
        *_ctrader_message_name_sources(trader_res),
        *_ctrader_message_name_sources(trader),
        ignored_values=ctx.ignored_name_values,
    )


def _handle_ctrader_trader_error(error_text: str, host: str, account_id: int) -> str:
    if _is_expected_ctrader_host_route_error(error_text):
        logger.debug(
            "cTrader trader details unavailable on this host",
            extra={
                "data": {
                    "account_id": str(account_id),
                    "host": host,
                    "error": error_text,
                }
            },
        )
        return "keep"
    if _is_ctrader_account_not_authorized_error(error_text):
        logger.info(
            "Account %s not authorized for token, skipping",
            account_id,
            extra={
                "data": {
                    "account_id": str(account_id),
                    "host": host,
                    "error": error_text,
                }
            },
        )
        return "skip"
    logger.warning(
        "Could not load cTrader trader details for discovered account",
        extra={
            "data": {
                "account_id": str(account_id),
                "host": host,
                "error": error_text,
            }
        },
    )
    return "keep"


def _build_ctrader_live_account(
    host: str,
    sock: ssl.SSLSocket,
    access_token: str,
    account_item: Any,
) -> LiveAccount | None:
    logger.debug(
        "Discovered raw cTrader account payload",
        extra={
            "data": {
                "host": host,
                "account_fields": _message_to_debug_fields(account_item),
            }
        },
    )
    account_id = _ctrader_resolved_account_id(account_item)
    if account_id <= 0:
        _log_ctrader_account_missing_ctid(host, account_item)
        return None
    is_demo = not bool(getattr(account_item, "isLive", False))
    if not _ctrader_host_matches_environment(host=host, is_live=not is_demo):
        _log_ctrader_host_environment_mismatch(host, account_id, is_demo)
        return None
    ctx = _ctrader_account_context(host, sock, access_token, account_id, account_item)
    try:
        _request_ctrader_account_detail(ctx, account_id)
        _load_ctrader_trader_details(ctx, account_id)
    except _CtraderStreamError:
        raise
    except Exception as exc:
        error_text = repr(exc)
        if _handle_ctrader_trader_error(error_text, host, account_id) == "skip":
            return None
    account_name = _ctrader_account_name(account_id=account_id, account_label=ctx.account_name_hint)
    return LiveAccount(
        id=str(account_id),
        name=account_name,
        broker="ctrader",
        balance=ctx.balance,
        is_demo=is_demo,
        broker_login=ctx.trader_login or None,
        ctid=str(account_id),
    )


async def _discover_ctrader_accounts_on_host(
    *,
    host: str,
    app_id: str,
    app_secret: str,
    access_token: str,
) -> list[LiveAccount]:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((host, 5035), timeout=12) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as sock:
            _authenticate_ctrader_application(sock, app_id=app_id, app_secret=app_secret)
            list_res = _request_ctrader_account_list(sock, access_token=access_token)

            accounts: list[LiveAccount] = []
            account_items = sorted(
                list_res.ctidTraderAccount,
                key=lambda item: int(getattr(item, "ctidTraderAccountId", 0) or 0),
            )
            for account_item in account_items:
                account = _build_ctrader_live_account(host, sock, access_token, account_item)
                if account is None:
                    continue
                accounts.append(account)
            return accounts

async def discover_ctrader_accounts(
    app_id: str, app_secret: str, access_token: str
) -> list[LiveAccount]:
    """Discover all cTrader accounts linked to an access token."""
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    access_token = access_token.strip()
    if not app_id or not app_secret or not access_token:
        return []

    hosts = ["demo.ctraderapi.com", "live.ctraderapi.com"]
    by_id: dict[str, LiveAccount] = {}
    host_preference_rank = {"demo.ctraderapi.com": 0, "live.ctraderapi.com": 1}
    source_rank_by_id: dict[str, int] = {}
    for host in hosts:
        try:
            discovered = await _discover_ctrader_accounts_on_host(
                host=host,
                app_id=app_id,
                app_secret=app_secret,
                access_token=access_token,
            )
        except Exception as exc:
            error_text = repr(exc)
            if _is_expected_ctrader_host_route_error(error_text):
                logger.info(
                    "cTrader account discovery unavailable on host; skipping environment-incompatible endpoint",
                    extra={"data": {"host": host, "error": error_text}},
                )
            else:
                logger.warning(
                    "cTrader account discovery failed on host",
                    extra={"data": {"host": host, "error": error_text}},
                )
            continue
        for account in discovered:
            current_rank = source_rank_by_id.get(account.id, 1_000_000)
            next_rank = host_preference_rank.get(host, 1_000_000)
            if account.id in by_id and next_rank >= current_rank:
                continue
            by_id[account.id] = account
            source_rank_by_id[account.id] = next_rank

    return sorted(by_id.values(), key=lambda item: item.id)


async def discover_configured_accounts(
    accounts_env: Path = DEFAULT_ACCOUNTS_ENV,
) -> list[LiveAccount]:
    # File-first precedence: .accounts.env wins over the process environment.
    # Discovery also must not mutate os.environ (no load_dotenv side effects).
    env = load_accounts_env_values(accounts_env)

    discovered_accounts: dict[tuple[str, str], LiveAccount] = {}

    ctrader_app_id = str(env.get("CTRADER_APP_ID", "")).strip()
    ctrader_app_secret = str(env.get("CTRADER_APP_SECRET", "")).strip()
    ctrader_access_token = str(env.get("CTRADER_ACCESS_TOKEN", "")).strip()
    if ctrader_app_id and ctrader_app_secret and ctrader_access_token:
        try:
            ctrader_accounts = await discover_ctrader_accounts(
                ctrader_app_id, ctrader_app_secret, ctrader_access_token
            )
            for account in ctrader_accounts:
                discovered_accounts[(account.broker, account.id)] = account
        except Exception as exc:
            logger.warning(
                "Skipping cTrader account discovery because of runtime error",
                extra={"data": {"error": repr(exc)}},
            )

    for credentials in _iter_alpaca_credentials(env):
        try:
            accounts = discover_alpaca_accounts(
                credentials["api_key"],
                credentials["secret_key"],
                bool(credentials["paper"]),
            )
        except Exception as exc:
            logger.warning(
                "Skipping Alpaca account discovery for credential block",
                extra={
                    "data": {
                        "block": credentials["key"],
                        "error": repr(exc),
                    }
                },
            )
            continue
        for account in accounts:
            discovered_accounts[(account.broker, account.id)] = account

    return sorted(
        discovered_accounts.values(),
        key=lambda item: (item.broker, item.name.lower(), item.id),
    )
