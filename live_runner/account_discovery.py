from __future__ import annotations

import math
import os
import re
import socket
import ssl
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from live_runner.alpaca_adapter import (
    _alpaca_acc_indexes,
    _alpaca_credential_block,
    _alpaca_credential_blocks,
    _default_alpaca_credentials,
)
from live_runner.credential_env import (
    load_accounts_env_values,
    parse_bool_env,
    resolve_env_value,
)
from shared.database import (
    demo_verification_age_seconds,
    record_live_account_demo_verification,
)
from shared.logging import get_logger
from shared.models import LiveAccount

logger = get_logger("live-account-discovery")

_HONOUR_ZERO_FLAG = "CTRADER_HONOUR_ZERO_MONEY_DIGITS"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_ENV = PROJECT_ROOT / ".accounts.env"

_ALPACA_ACC_RE = re.compile(r"^ALPACA_API_KEY_ACC(?P<index>\d+)$")


class CTraderUnavailableError(RuntimeError):
    pass


CTRADER_UNAVAILABLE_STATUS = "unavailable"
_Protobuf: Any = None
_OpenApiMessages_pb2: Any = None
_ProtoHeartbeatEvent: Any = None
_ProtoMessage: Any = None
_ProtoOAAccountAuthReq: Any = None
_ProtoOAAccountAuthRes: Any = None
_ProtoOAApplicationAuthReq: Any = None
_ProtoOAApplicationAuthRes: Any = None
_ProtoOAErrorRes: Any = None
_ProtoOAGetAccountListByAccessTokenReq: Any = None
_ProtoOAGetAccountListByAccessTokenRes: Any = None
_ProtoOATraderReq: Any = None
_ProtoOATraderRes: Any = None
_PROTO_OA_CTID_TRADER_ACCOUNT_REQ: Any = None
_PROTO_OA_CTID_TRADER_ACCOUNT_RES: Any = None


def _load_ctrader_protocol() -> None:
    global _OpenApiMessages_pb2
    global _ProtoHeartbeatEvent
    global _ProtoMessage
    global _ProtoOAAccountAuthReq
    global _ProtoOAAccountAuthRes
    global _ProtoOAApplicationAuthReq
    global _ProtoOAApplicationAuthRes
    global _ProtoOAErrorRes
    global _ProtoOAGetAccountListByAccessTokenReq
    global _ProtoOAGetAccountListByAccessTokenRes
    global _ProtoOATraderReq
    global _ProtoOATraderRes
    global _PROTO_OA_CTID_TRADER_ACCOUNT_REQ
    global _PROTO_OA_CTID_TRADER_ACCOUNT_RES
    global _Protobuf
    if _Protobuf is not None:
        return
    try:
        from ctrader_open_api import Protobuf
        from ctrader_open_api.messages import OpenApiMessages_pb2
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
            ProtoHeartbeatEvent,
            ProtoMessage,
        )
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
    except ImportError as exc:
        raise CTraderUnavailableError(
            "cTrader account discovery requires the optional ctrader extra."
        ) from exc
    _Protobuf = Protobuf
    _OpenApiMessages_pb2 = OpenApiMessages_pb2
    _ProtoHeartbeatEvent = ProtoHeartbeatEvent
    _ProtoMessage = ProtoMessage
    _ProtoOAAccountAuthReq = ProtoOAAccountAuthReq
    _ProtoOAAccountAuthRes = ProtoOAAccountAuthRes
    _ProtoOAApplicationAuthReq = ProtoOAApplicationAuthReq
    _ProtoOAApplicationAuthRes = ProtoOAApplicationAuthRes
    _ProtoOAErrorRes = ProtoOAErrorRes
    _ProtoOAGetAccountListByAccessTokenReq = ProtoOAGetAccountListByAccessTokenReq
    _ProtoOAGetAccountListByAccessTokenRes = ProtoOAGetAccountListByAccessTokenRes
    _ProtoOATraderReq = ProtoOATraderReq
    _ProtoOATraderRes = ProtoOATraderRes
    _PROTO_OA_CTID_TRADER_ACCOUNT_REQ = getattr(
        OpenApiMessages_pb2, "ProtoOACtidTraderAccountReq", None
    )
    _PROTO_OA_CTID_TRADER_ACCOUNT_RES = getattr(
        OpenApiMessages_pb2, "ProtoOACtidTraderAccountRes", None
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
    """Enumerate Alpaca credential sets using the adapter's block parser.

    Sharing one parser (live_runner.alpaca_adapter) keeps the discovered
    `paper` flag identical to the mode the runtime adapter is constructed
    with, so is_demo can never contradict the actual trading mode.
    """
    indexed: list[dict[str, Any]] = []
    for block in _alpaca_credential_blocks(env, _alpaca_acc_indexes(env)):
        indexed.append(
            {
                "key": str(block.get("source_index", "")),
                "api_key": str(block["api_key"]),
                "secret_key": str(block["secret_key"]),
                "paper": bool(block["paper"]),
            }
        )

    if indexed:
        return indexed

    default_credentials = _default_alpaca_credentials(env)
    if default_credentials is None:
        return []
    api_key, secret_key, paper = default_credentials
    return [
        {
            "key": "DEFAULT",
            "api_key": api_key,
            "secret_key": secret_key,
            "paper": paper,
        }
    ]


def _iter_alpaca_credentials_safe(env: dict[str, str]) -> list[dict[str, Any]]:
    """Enumerate credential sets block-by-block so a strict-parse error (e.g. a
    typo'd ALPACA_PAPER_ACC{N}) skips that block instead of aborting the whole
    discovery run, which would also drop accounts from the other broker."""
    blocks: list[dict[str, Any]] = []
    for idx in _alpaca_acc_indexes(env):
        try:
            block = _alpaca_credential_block(env, idx)
        except ValueError as exc:
            logger.warning(
                "Skipping malformed Alpaca credential block during discovery",
                extra={"data": {"block": f"ACC{idx}", "error": str(exc)}},
            )
            continue
        if block is None:
            continue
        blocks.append(
            {
                "key": str(block.get("source_index", "")),
                "api_key": str(block["api_key"]),
                "secret_key": str(block["secret_key"]),
                "paper": bool(block["paper"]),
            }
        )

    if blocks:
        return blocks

    try:
        default_credentials = _default_alpaca_credentials(env)
    except ValueError as exc:
        logger.warning(
            "Skipping malformed default Alpaca credentials during discovery",
            extra={"data": {"error": str(exc)}},
        )
        return []
    if default_credentials is None:
        return []
    api_key, secret_key, paper = default_credentials
    return [
        {
            "key": "DEFAULT",
            "api_key": api_key,
            "secret_key": secret_key,
            "paper": paper,
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


def _honour_zero_money_digits() -> bool:
    """Whether a broker-sent `moneyDigits` of 0 is honoured. Default: no.

    Read from the environment on every call rather than cached at import, so a test can exercise
    both settings and an operator can change it without a code change.

    Parsed with the repo's single boolean parser rather than a hand-rolled token set: this was
    already written out by hand once, and it disagreed with `TRUE_BOOL_TOKENS` (dropping `y`) while
    treating a typo as *off* rather than raising. `parse_bool_env` is documented as the only
    definition precisely because four agreeing copies once cost this repository two live bugs.
    Absent means the default; present-but-blank is an error.

    Why a flag and not simply the fix: `HasField` separates "the broker sent nothing" from "the
    broker means zero decimals", and honouring the second is what the range guard below already
    implies. But if some cTrader build *always sets* the field to 0 rather than omitting it, every
    account would read as 0-decimal and every balance would be 100x too *large* instead of too
    small - the mirror of today's bug, and worse, because it would hit every account rather than
    none. The log distinguishes the two cases at runtime; this flag turns on the behaviour once
    that evidence exists. Backlog item 6.
    """
    configured = _HONOUR_ZERO_FLAG in os.environ
    return parse_bool_env(
        _HONOUR_ZERO_FLAG,
        resolve_env_value(None, _HONOUR_ZERO_FLAG) if configured else None,
        default=False,
    )


def _resolve_money_digits(trader: Any, *, context: str) -> int:
    """The broker's decimal-place count, defaulting to 2, and saying so when it guesses.

    `moneyDigits` is a protobuf `uint32` **with presence**, so an unset field reads as `0`, not
    `None`. `int(getattr(trader, "moneyDigits", 2) or 2)` therefore cannot tell two situations
    apart:

    - the broker sent **no** `moneyDigits` at all, and 2 is the right conventional default, and
    - the broker sent a genuine **0**, meaning this account has no minor units, in which case
      dividing the balance by 100 makes it a hundred times too small.

    The line below it, `scale = 10**money_digits if 0 <= money_digits <= 12 else 100`, **admits
    zero** - so the range guard and the `or 2` above it contradict each other, and the guard's
    acceptance of zero is dead code.

    Which of the two is happening is not knowable from the value alone, but it *is* knowable from
    `HasField`. So the ambiguous case is logged with the presence flag, which settles it on the
    next successful broker call without changing a single computed value. That is the whole of
    what this function does beyond the arithmetic; honouring a real zero is backlog item 6 and
    needs the answer first.

    Duplicated deliberately in `ctrader_adapter.py` rather than shared, because importing
    `account_discovery` from the cTrader adapter would drag in the Alpaca adapter's dependency
    chain. `test_both_copies_of_the_balance_scaling_are_identical` holds the two in sync.
    """
    raw = getattr(trader, "moneyDigits", 2)
    has_field = getattr(trader, "HasField", None)
    present = None
    if has_field is not None:
        try:
            present = bool(has_field("moneyDigits"))
        except (TypeError, ValueError):
            present = None
    if not raw:  # the `or 2` collapse is about to happen
        if present and _honour_zero_money_digits():
            logger.info(
                "moneyDigits explicitly 0 (field present); honouring it, so no scaling is "
                "applied - %s",
                context,
            )
            return 0
        logger.warning(
            "moneyDigits is %r (field present: %s); falling back to 2 decimal places. If the "
            "broker sent a genuine 0 this balance is 100x too small - %s",
            raw,
            "unknown" if present is None else present,
            context,
        )
        return 2
    return int(raw)


def _trader_balance(trader: Any) -> float:
    money_digits = _resolve_money_digits(trader, context="account discovery")
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
            # `int(candidate) <= 0` cannot be tested as written, because a negative login
            # carries a sign and `"-1".isdigit()` is False - so the `<= 0` half never ran for
            # the values it was written for, and the guard refused `0` while passing `-1`.
            # That is not a judgement call: `traderLogin` is an int64, so a negative is an
            # ordinary wire value, and every validator downstream of here (`preflight`,
            # `account_routing`) already rejects a non-positive identifier with a bare
            # `int(...) <= 0` and no `isdigit()` conjunct. Test the magnitude and keep the
            # sign out of it. Backlog item 3.
            magnitude = candidate.lstrip("+-")
            if magnitude.isdigit() and (int(magnitude) == 0 or candidate.startswith("-")):
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
            from google.protobuf.json_format import MessageToDict

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
    _load_ctrader_protocol()
    envelope = _ProtoMessage(
        payloadType=int(message.payloadType),
        payload=message.SerializeToString(),
        clientMsgId=client_msg_id,
    )
    raw = envelope.SerializeToString()
    sock_obj.sendall(struct.pack(">I", len(raw)) + raw)


def _ctrader_wait_response(
    sock_obj: ssl.SSLSocket, client_msg_id: str, timeout_seconds: float
) -> Any:
    _load_ctrader_protocol()
    deadline = time.time() + timeout_seconds
    heartbeat_payload_type = _ProtoHeartbeatEvent().payloadType

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError()
        sock_obj.settimeout(remaining)
        frame_size = struct.unpack(">I", _ctrader_recv_exact(sock_obj, 4))[0]
        frame_payload = _ctrader_recv_exact(sock_obj, frame_size)
        envelope = _ProtoMessage()
        envelope.ParseFromString(frame_payload)

        if envelope.payloadType == heartbeat_payload_type:
            heartbeat = _ProtoHeartbeatEvent()
            _ctrader_send_message(sock_obj, heartbeat, "")
            continue

        payload = _Protobuf.extract(envelope)
        if envelope.clientMsgId != client_msg_id:
            continue
        if isinstance(payload, _ProtoOAErrorRes):
            raise RuntimeError(f"{payload.errorCode}: {payload.description}")
        return payload


def _authenticate_ctrader_application(
    sock: ssl.SSLSocket,
    *,
    app_id: str,
    app_secret: str,
) -> None:
    app_auth_req = _ProtoOAApplicationAuthReq()
    app_auth_req.clientId = app_id
    app_auth_req.clientSecret = app_secret
    app_auth_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(sock, app_auth_req, app_auth_client_msg_id)
    app_auth_res = _ctrader_wait_response(sock, app_auth_client_msg_id, 20)
    if not isinstance(app_auth_res, _ProtoOAApplicationAuthRes):
        raise RuntimeError(f"Unexpected application auth response: {type(app_auth_res).__name__}")


def _request_ctrader_account_list(
    sock: ssl.SSLSocket,
    *,
    access_token: str,
) -> Any:
    list_req = _ProtoOAGetAccountListByAccessTokenReq()
    list_req.accessToken = access_token
    list_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(sock, list_req, list_client_msg_id)
    list_res = _ctrader_wait_response(sock, list_client_msg_id, 20)
    if not isinstance(list_res, _ProtoOAGetAccountListByAccessTokenRes):
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
    except OSError as exc:
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
    _load_ctrader_protocol()
    account_auth_req = _ProtoOAAccountAuthReq()
    account_auth_req.ctidTraderAccountId = account_id
    account_auth_req.accessToken = ctx.access_token
    account_auth_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(ctx.sock, account_auth_req, account_auth_client_msg_id)
    account_auth_res = _ctrader_wait_response(ctx.sock, account_auth_client_msg_id, 20)
    if not isinstance(account_auth_res, _ProtoOAAccountAuthRes):
        raise RuntimeError(f"Unexpected account auth response: {type(account_auth_res).__name__}")

    trader_req = _ProtoOATraderReq()
    trader_req.ctidTraderAccountId = account_id
    trader_client_msg_id = str(uuid.uuid4())
    _ctrader_send_message(ctx.sock, trader_req, trader_client_msg_id)
    trader_res = _ctrader_wait_response(ctx.sock, trader_client_msg_id, 20)
    if not isinstance(trader_res, _ProtoOATraderRes):
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
    _load_ctrader_protocol()

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
                    "cTrader account discovery unavailable on host; "
                    "skipping environment-incompatible endpoint",
                    extra={
                        "data": {
                            "broker": "ctrader",
                            "status": CTRADER_UNAVAILABLE_STATUS,
                            "host": host,
                            "error": error_text,
                        }
                    },
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
        except CTraderUnavailableError as exc:
            logger.warning(
                "cTrader account discovery unavailable",
                extra={
                    "data": {
                        "broker": "ctrader",
                        "status": CTRADER_UNAVAILABLE_STATUS,
                        "error": str(exc),
                    }
                },
            )
        except Exception as exc:
            logger.warning(
                "Skipping cTrader account discovery because of runtime error",
                extra={"data": {"error": repr(exc)}},
            )

    for credentials in _iter_alpaca_credentials_safe(env):
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


def record_discovered_account_verification(
    account_id: str,
    *,
    is_demo: bool | None,
    ctid: str | None,
    broker_login: str | None,
) -> None:
    """Persist a broker demo verification for a freshly discovered account.

    Lives here rather than in the viewer's request path because **two processes
    need it and only one of them was calling it**. The daemon enforces
    `DEMO_VERIFICATION_TTL_SECONDS` before every broker submission, but the only
    writer of the stamp was the viewer's account-discovery endpoint. The daemon is
    a separate systemd unit, so a headless deployment - no browser tab holding
    `/ws/live/accounts`, nobody calling `GET /api/live/accounts` - aged every
    account out and then silently refused every dispatch, while the deployment row
    still read `ACTIVE` and the adapter was still built, because that path checks
    the `is_demo` flag rather than the age.

    Discovery is the only place that has just spoken to the broker, so it is the
    only legitimate writer. Failure is logged and swallowed: discovery must still
    return the account list even when the write fails, and the account then simply
    stays unverified (fail-closed) until the next successful discovery.

    `is_demo` is tri-state. `None` means the source did not report the flag at
    all, and then there is no verification to record: defaulting it to False would
    invent a "the broker says this is live" confirmation out of an absent field,
    and would stamp a live-looking verification onto the account besides.
    """
    if is_demo is None:
        logger.warning(
            "Discovery reported no is_demo flag; recording no demo verification",
            extra={"data": {"account_id": account_id}},
        )
        return
    try:
        record_live_account_demo_verification(
            account_id,
            is_demo=is_demo,
            ctid=ctid,
            broker_login=broker_login,
        )
    except Exception as exc:
        logger.warning(
            "Could not persist broker demo verification for account",
            extra={"data": {"account_id": account_id, "error": repr(exc)}},
        )


async def refresh_account_demo_verifications() -> int:
    """Re-ask the broker about every configured account and record the answers.

    Returns the number of verifications written, for logging. Never raises: a
    refresh failure must not stop the daemon's loop, and the accounts simply stay
    as they were - which for a stale stamp means dispatch keeps being refused,
    i.e. fail-closed.

    This is the daemon's half of the TTL. The viewer refreshes on demand, which
    is the right behaviour for an operator watching a screen and useless as the
    only mechanism: nothing about running headless should change whether the
    process that places orders is allowed to place them.
    """
    try:
        discovered = await discover_configured_accounts()
    except Exception as exc:
        logger.warning(
            "Broker demo re-verification failed; accounts keep their current state",
            extra={"data": {"error": repr(exc)}},
        )
        return 0

    written = 0
    skipped = 0
    for account in discovered:
        # `discover_configured_accounts` returns `list[LiveAccount]` - pydantic
        # models, not dicts. The dict-only guard this replaces was copied from the
        # viewer's `_discover_live_accounts_payload`, where it is correct because
        # *that* function receives dicts. Here it rejected every real account, so
        # this function returned 0 forever: the TTL it exists to renew expired 900s
        # after the last viewer-side discovery and the daemon refused every
        # dispatch - the exact failure it was committed to fix, still in place.
        # The tests missed it because they stubbed dicts, the shape this function
        # never receives.
        #
        # Both shapes are handled so neither can silently stop working, and a
        # wholly unexplained zero is logged below.
        if isinstance(account, dict):
            account_id = str(account.get("id") or "").strip()
            raw_is_demo = account.get("is_demo")
            raw_login = account.get("broker_login") or account.get("login")
            raw_ctid = account.get("ctid")
        else:
            account_id = str(getattr(account, "id", "") or "").strip()
            raw_is_demo = getattr(account, "is_demo", None)
            raw_login = getattr(account, "broker_login", None)
            raw_ctid = getattr(account, "ctid", None)
        if not account_id:
            skipped += 1
            continue
        is_demo = raw_is_demo if isinstance(raw_is_demo, bool) else None
        broker_login = str(raw_login or "").strip() or None
        ctid = str(raw_ctid or "").strip() or None
        before = demo_verification_age_seconds(account_id)
        record_discovered_account_verification(
            account_id,
            is_demo=is_demo,
            ctid=ctid,
            broker_login=broker_login,
        )
        if demo_verification_age_seconds(account_id) != before:
            written += 1
    if discovered and written == 0 and skipped == 0:
        # Every account was understood and none produced a new stamp. Either
        # the writes are failing, or the shape changed again. Either way it is
        # worth a line in the log rather than a silent 0 - a refresher that
        # reports zero with no explanation is exactly how the first version of
        # this function hid a no-op for a whole session.
        logger.warning(
            "Broker discovery returned accounts but no demo verification was "
            "recorded; stamps stay as they are",
            extra={"data": {"discovered": len(discovered)}},
        )
    if written:
        logger.info(
            "Refreshed broker demo verification for account(s)",
            extra={"data": {"written": written}},
        )
    return written
