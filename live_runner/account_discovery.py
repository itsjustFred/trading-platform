from __future__ import annotations

import math
import os
import re
import socket
import ssl
import struct
import time
import uuid
from pathlib import Path
from typing import Any

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
from dotenv import load_dotenv

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
        )
    ]


def _trader_balance(trader: Any) -> float:
    money_digits = int(getattr(trader, "moneyDigits", 2) or 2)
    scale = 10**money_digits if 0 <= money_digits <= 12 else 100
    return _safe_float(getattr(trader, "balance", 0.0), default=0.0) / scale


def _ctrader_mode_label(is_demo: bool) -> str:
    return "Demo" if is_demo else "Live"


def _ctrader_broker_label(broker_name: str) -> str:
    normalized = broker_name.strip()
    return normalized if normalized else "cTrader"


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


def _ctrader_extract_account_label(
    *sources: Any, ignored_values: set[str] | None = None
) -> str:
    ignored = {value.strip() for value in (ignored_values or set()) if value and value.strip()}
    for source in sources:
        if source is None:
            continue
        for field_name in _CTRADER_ACCOUNT_NAME_FIELDS:
            raw_value = getattr(source, field_name, "")
            candidate = str(raw_value or "").strip()
            if not candidate:
                continue
            if candidate in ignored:
                continue
            if candidate.isdigit():
                continue
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


def _ctrader_account_name(
    *,
    broker_name: str,
    account_label: str,
    is_demo: bool,
    fallback_counters: dict[tuple[str, bool], int],
) -> str:
    resolved_name = account_label.strip()
    if resolved_name:
        return resolved_name

    broker_label = _ctrader_broker_label(broker_name)
    mode_label = _ctrader_mode_label(is_demo)

    counter_key = (broker_label, is_demo)
    fallback_index = fallback_counters.get(counter_key, 0) + 1
    fallback_counters[counter_key] = fallback_index
    return f"{broker_label} {mode_label} #{fallback_index}"


async def _discover_ctrader_accounts_on_host(
    *,
    host: str,
    app_id: str,
    app_secret: str,
    access_token: str,
) -> list[LiveAccount]:
    def _recv_exact(sock_obj: ssl.SSLSocket, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = sock_obj.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("cTrader socket closed unexpectedly.")
            buf += chunk
        return buf

    def _send_message(sock_obj: ssl.SSLSocket, message: Any, client_msg_id: str) -> None:
        envelope = ProtoMessage(
            payloadType=int(message.payloadType),
            payload=message.SerializeToString(),
            clientMsgId=client_msg_id,
        )
        raw = envelope.SerializeToString()
        sock_obj.sendall(struct.pack(">I", len(raw)) + raw)

    def _wait_response(
        sock_obj: ssl.SSLSocket, client_msg_id: str, timeout_seconds: float
    ) -> Any:
        deadline = time.time() + timeout_seconds
        heartbeat_payload_type = ProtoHeartbeatEvent().payloadType

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError()
            sock_obj.settimeout(remaining)
            frame_size = struct.unpack(">I", _recv_exact(sock_obj, 4))[0]
            frame_payload = _recv_exact(sock_obj, frame_size)
            envelope = ProtoMessage()
            envelope.ParseFromString(frame_payload)

            if envelope.payloadType == heartbeat_payload_type:
                heartbeat = ProtoHeartbeatEvent()
                _send_message(sock_obj, heartbeat, "")
                continue

            payload = Protobuf.extract(envelope)
            if envelope.clientMsgId != client_msg_id:
                continue
            if isinstance(payload, ProtoOAErrorRes):
                raise RuntimeError(f"{payload.errorCode}: {payload.description}")
            return payload

    context = ssl.create_default_context()
    with socket.create_connection((host, 5035), timeout=12) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as sock:
            app_auth_req = ProtoOAApplicationAuthReq()
            app_auth_req.clientId = app_id
            app_auth_req.clientSecret = app_secret
            app_auth_client_msg_id = str(uuid.uuid4())
            _send_message(sock, app_auth_req, app_auth_client_msg_id)
            app_auth_res = _wait_response(sock, app_auth_client_msg_id, 20)
            if not isinstance(app_auth_res, ProtoOAApplicationAuthRes):
                raise RuntimeError(
                    f"Unexpected application auth response: {type(app_auth_res).__name__}"
                )

            list_req = ProtoOAGetAccountListByAccessTokenReq()
            list_req.accessToken = access_token
            list_client_msg_id = str(uuid.uuid4())
            _send_message(sock, list_req, list_client_msg_id)
            list_res = _wait_response(sock, list_client_msg_id, 20)
            if not isinstance(list_res, ProtoOAGetAccountListByAccessTokenRes):
                raise RuntimeError(f"Unexpected account list response: {type(list_res).__name__}")

            accounts: list[LiveAccount] = []
            fallback_name_counters: dict[tuple[str, bool], int] = {}
            account_items = sorted(
                list_res.ctidTraderAccount,
                key=lambda item: int(getattr(item, "ctidTraderAccountId", 0) or 0),
            )
            for account_item in account_items:
                account_id = int(account_item.ctidTraderAccountId)
                is_demo = not bool(getattr(account_item, "isLive", False))
                balance = 0.0
                broker_name = _ctrader_extract_broker_name(
                    *_ctrader_message_name_sources(account_item)
                )
                trader_login = int(getattr(account_item, "traderLogin", 0) or 0)
                ignored_name_values = {str(account_id)}
                if trader_login > 0:
                    ignored_name_values.add(str(trader_login))
                account_name_hint = _ctrader_extract_account_label(
                    *_ctrader_message_name_sources(account_item),
                    ignored_values=ignored_name_values,
                )

                try:
                    if _PROTO_OA_CTID_TRADER_ACCOUNT_REQ is not None:
                        try:
                            account_detail_req = _PROTO_OA_CTID_TRADER_ACCOUNT_REQ()
                            if hasattr(account_detail_req, "ctidTraderAccountId"):
                                account_detail_req.ctidTraderAccountId = account_id
                            if hasattr(account_detail_req, "accessToken"):
                                account_detail_req.accessToken = access_token
                            account_detail_client_msg_id = str(uuid.uuid4())
                            _send_message(sock, account_detail_req, account_detail_client_msg_id)
                            account_detail_res = _wait_response(
                                sock, account_detail_client_msg_id, 20
                            )
                            if _PROTO_OA_CTID_TRADER_ACCOUNT_RES is None or isinstance(
                                account_detail_res, _PROTO_OA_CTID_TRADER_ACCOUNT_RES
                            ):
                                broker_name = broker_name or _ctrader_extract_broker_name(
                                    *_ctrader_message_name_sources(account_detail_res)
                                )
                                if not account_name_hint:
                                    account_name_hint = _ctrader_extract_account_label(
                                        *_ctrader_message_name_sources(account_detail_res),
                                        ignored_values=ignored_name_values,
                                    )
                        except Exception as exc:
                            logger.debug(
                                "Optional cTrader account detail request failed",
                                extra={
                                    "data": {
                                        "account_id": str(account_id),
                                        "host": host,
                                        "error": repr(exc),
                                    }
                                },
                            )

                    account_auth_req = ProtoOAAccountAuthReq()
                    account_auth_req.ctidTraderAccountId = account_id
                    account_auth_req.accessToken = access_token
                    account_auth_client_msg_id = str(uuid.uuid4())
                    _send_message(sock, account_auth_req, account_auth_client_msg_id)
                    account_auth_res = _wait_response(sock, account_auth_client_msg_id, 20)
                    if not isinstance(account_auth_res, ProtoOAAccountAuthRes):
                        raise RuntimeError(
                            f"Unexpected account auth response: {type(account_auth_res).__name__}"
                        )

                    trader_req = ProtoOATraderReq()
                    trader_req.ctidTraderAccountId = account_id
                    trader_client_msg_id = str(uuid.uuid4())
                    _send_message(sock, trader_req, trader_client_msg_id)
                    trader_res = _wait_response(sock, trader_client_msg_id, 20)
                    if isinstance(trader_res, ProtoOATraderRes):
                        trader = trader_res.trader
                        balance = _trader_balance(trader)
                        broker_name = broker_name or _ctrader_extract_broker_name(
                            *_ctrader_message_name_sources(trader_res),
                            *_ctrader_message_name_sources(trader),
                        )
                        resolved_login = int(getattr(trader, "traderLogin", 0) or 0)
                        if resolved_login > 0:
                            trader_login = resolved_login
                            ignored_name_values.add(str(resolved_login))
                        account_name_hint = account_name_hint or _ctrader_extract_account_label(
                            *_ctrader_message_name_sources(trader_res),
                            *_ctrader_message_name_sources(trader),
                            ignored_values=ignored_name_values,
                        )
                except Exception as exc:
                    logger.warning(
                        "Could not load cTrader trader details for discovered account",
                        extra={
                            "data": {
                                "account_id": str(account_id),
                                "host": host,
                                "error": repr(exc),
                            }
                        },
                    )

                account_name = _ctrader_account_name(
                    broker_name=broker_name,
                    account_label=account_name_hint,
                    is_demo=is_demo,
                    fallback_counters=fallback_name_counters,
                )
                accounts.append(
                    LiveAccount(
                        id=str(account_id),
                        name=account_name,
                        broker="ctrader",
                        balance=balance,
                        is_demo=is_demo,
                    )
                )
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
            logger.warning(
                "cTrader account discovery failed on host",
                extra={"data": {"host": host, "error": repr(exc)}},
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
    load_dotenv(accounts_env)
    env = dict(os.environ)

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
