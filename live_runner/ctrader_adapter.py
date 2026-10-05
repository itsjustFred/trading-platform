from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import os
import re
import tempfile
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    BaseLiveAdapter,
    coerce_positive_live_qty,
    is_exit_only_metadata,
    is_unbounded_tp,
)
from live_runner.credential_env import (
    FALSE_BOOL_TOKENS,
    TRUE_BOOL_TOKENS,
    parse_bool_env,
    resolve_env_value,
    resolve_flag_env,
)
from shared.database import (
    find_live_signal_id_by_broker_order_id,
    get_live_signal_broker_order_id,
    get_live_signal_metadata,
    get_live_signal_reconciliation_cursor,
    get_live_signal_timestamp,
    list_live_signal_ids_for_reconciliation,
    save_live_execution,
    save_live_signal,
    set_live_signal_broker_order_id,
)
from shared.logging import get_logger
from shared.models import (
    SL_METADATA_KEYS,
    TP_METADATA_KEYS,
    LiveExecution,
    LiveSignal,
)

logger = get_logger("live-runner")

_HONOUR_ZERO_FLAG = "CTRADER_HONOUR_ZERO_MONEY_DIGITS"

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


_TRUE_BOOL_TOKENS = TRUE_BOOL_TOKENS
_FALSE_BOOL_TOKENS = FALSE_BOOL_TOKENS


def _parse_bool_env(name: str, value: str | None, *, default: bool) -> bool:
    """Delegate to the one definition in `live_runner.credential_env`.

    A private copy of a strict boolean parser is how a *blanked* value ended up
    silently selecting a default in this repo - twice, once per broker, and the
    default was demo mode. Four copies that agree today is the same hazard as
    every other duplicate this audit has found; the difference is that this rule
    has already produced two live bugs. See the note in `credential_env`.
    """
    return parse_bool_env(name, value, default=default)


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


def _first_metadata_float(
    metadata: Mapping[str, Any], keys: tuple[str, ...]
) -> float | None:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid {key}: {value!r}") from exc
        return parsed
    return None
# ── cTrader volume unit contract ───────────────────────────────────────────────
#
# ONE unit contract for every quantity in the cTrader path:
#
#   * Signal metadata 'qty' is in 0.01-LOT units of the symbol, i.e.
#     broker_volume / volume_per_qty. The risk engine sizes in these units
#     (position_size = risk_amount / (risk_distance * pip_value)).
#     volume_per_qty = lotSize / 100 from the broker symbol properties
#     (FX: 100000/100 = 1000, pinned by contract tests), else the static
#     fallback table per symbol class (FX/JPY/XAU/XAG/index).
#   * Raw cTrader protocol fields (ProtoOANewOrderReq.volume,
#     tradeData.volume, deal.filledVolume, order.executedVolume) are in
#     BROKER VOLUME UNITS: broker_volume == qty * volume_per_qty.
#
# Every crossing of that boundary goes through _qty_to_broker_volume /
# _broker_volume_to_qty with the symbol's scale (adapter._scale_for, #80:
# broker props first, fallback table second, unknown symbols raise).


#: Legacy FX scale, kept as the pinned default for the module-level helpers
#: and the FX fallback entry below (qty 5.0 -> volume 5000).
QTY_VOLUME_SCALE = 1000.0
CTRADER_RELATIVE_PRICE_UNIT_DIVISOR = 100_000
EXECUTION_RETRY_LIMIT = 3
UNRESOLVED_EXECUTION_EVENT_LIMIT = 1000


@dataclasses.dataclass(frozen=True)
class SymbolScale:
    """Per-symbol broker scaling (#80).

    ``volume_per_qty``: broker volume units per risk-engine qty unit.
    The qty unit is 0.01 lots, so ``volume_per_qty = lotSize / 100``
    (FX: 100000/100 = 1000, pinned by contract tests).
    ``price_digits``: broker-reported symbol price decimals retained as symbol
    metadata; relative SL/TP always uses the fixed protocol divisor.
    ``min_volume``: broker minimum order volume (fail-closed floor).
    ``source``: "broker" (ProtoOASymbol props) or "fallback" (static table).
    """

    volume_per_qty: float
    price_digits: int
    min_volume: int
    lot_size: float
    source: str = "fallback"


@dataclasses.dataclass(frozen=True)
class _FallbackScaleEntry:
    lot_size: float
    price_digits: int
    min_volume: int


#: Static per-symbol-class fallback table (#80, version 1). Used ONLY when the
#: broker exposes no symbol properties (offline/tests) — the broker-API
#: symbol properties (digits/lotSize/minVolume via ProtoOASymbolById) are the
#: source of truth whenever reachable. Values are cTrader conventions:
#: FX/JPY 1 lot = 100_000 base units; XAU 1 lot = 100 oz; XAG 1 lot = 5000 oz;
#: indices vary per symbol (lotSize=100 estimate, broker wins when reachable).
_FALLBACK_SCALES: dict[str, _FallbackScaleEntry] = {
    "fx": _FallbackScaleEntry(lot_size=100_000.0, price_digits=5, min_volume=1000),
    "jpy": _FallbackScaleEntry(lot_size=100_000.0, price_digits=3, min_volume=1000),
    "xau": _FallbackScaleEntry(lot_size=100.0, price_digits=2, min_volume=1),
    "xag": _FallbackScaleEntry(lot_size=5000.0, price_digits=3, min_volume=1),
    "index": _FallbackScaleEntry(lot_size=100.0, price_digits=2, min_volume=1),
}

#: Known index CFD symbols for fallback classification. Anything outside the
#: FX/JPY/XAU/XAG/index classes is UNKNOWN and fails closed (no trade instead
#: of a wrong size) — the broker props cover real symbols in production.
_INDEX_SYMBOLS = frozenset(
    {
        "US30", "NAS100", "SP500", "US500", "GER40", "DE40", "UK100",
        "FRA40", "JPN225", "AUS200", "EU50", "STOXX50", "HK50", "CN50",
        "IND50", "ES35", "IT40",
    }
)

#: Crypto bases that form 6-letter pairs (BTCUSD, ETHUSD, ...). Their lotSize
#: varies per coin and broker (BTC 1, altcoins differ), so no static fallback
#: is safe — they are UNKNOWN by design (broker props or no trade). Without
#: this guard they would fall into the currency-code FX branch at up to
#: 100000x the intended size.
_CRYPTO_BASES = frozenset(
    {
        "BTC", "ETH", "XRP", "LTC", "BCH", "DOT", "LINK", "XLM", "ADA",
        "DOGE", "SOL", "AVAX", "MATIC", "TRX", "ETC", "FIL", "ATOM",
        "NEAR", "ARB", "OP", "INJ", "SUI", "UNI", "APT", "HBAR", "VET",
        "ALGO", "SAND", "MANA", "XTZ", "EOS", "AAVE", "SNX", "ZEC",
        "DASH", "XMR", "BNB",
    }
)

_FX_CURRENCY_CODES = frozenset(
    {
        "AUD", "BRL", "CAD", "CHF", "CNH", "CNY", "CZK", "DKK", "EUR",
        "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN",
        "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "RUB", "SAR", "SEK",
        "SGD", "THB", "TRY", "USD", "ZAR",
    }
)


def _fallback_scale_class(symbol: str) -> str | None:
    """Classify *symbol* into a fallback scale class, or None when unknown."""
    upper = str(symbol or "").strip().upper()
    if not upper:
        return None
    if upper.startswith("XAU"):
        return "xau"
    if upper.startswith("XAG"):
        return "xag"
    if len(upper) == 6 and upper.isalpha():
        if upper[:3] in _CRYPTO_BASES:
            return None
        if upper[:3] not in _FX_CURRENCY_CODES or upper[3:] not in _FX_CURRENCY_CODES:
            return None
        if upper.endswith("JPY"):
            return "jpy"
        return "fx"
    if upper in _INDEX_SYMBOLS:
        return "index"
    return None


def _fallback_symbol_scale(symbol: str) -> SymbolScale:
    """Return the static fallback scale for *symbol*, failing closed (#80)."""
    cls = _fallback_scale_class(symbol)
    if cls is None:
        raise ValueError(
            f"Unknown cTrader symbol {symbol!r}: no broker properties cached and "
            "no fallback scale class (FX/JPY/XAU/XAG/index); refusing to guess "
            "a size or SL/TP scale."
        )
    entry = _FALLBACK_SCALES[cls]
    return SymbolScale(
        volume_per_qty=entry.lot_size / 100.0,
        price_digits=entry.price_digits,
        min_volume=entry.min_volume,
        lot_size=entry.lot_size,
        source="fallback",
    )


def _symbol_scale_from_broker_props(
    *,
    digits: int | None,
    lot_size: float | None,
    min_volume: int | None,
    fallback: SymbolScale | None,
) -> SymbolScale | None:
    """Build a broker-authoritative scale, using a fallback only when available."""
    try:
        digit_count = int(digits) if digits is not None else None
    except (TypeError, ValueError):
        digit_count = None
    if digit_count is None or not 0 <= digit_count <= 10:
        digit_count = fallback.price_digits if fallback is not None else None
    try:
        lots = float(lot_size) if lot_size is not None else None
    except (TypeError, ValueError):
        lots = None
    if lots is None or not math.isfinite(lots) or lots <= 0:
        lots = fallback.lot_size if fallback is not None else None
    try:
        minimum = int(min_volume) if min_volume is not None else None
    except (TypeError, ValueError):
        minimum = None
    if minimum is None or minimum <= 0:
        minimum = fallback.min_volume if fallback is not None else None
    if digit_count is None or lots is None or minimum is None:
        return None
    return SymbolScale(
        volume_per_qty=lots / 100.0,
        price_digits=digit_count,
        min_volume=minimum,
        lot_size=lots,
        source="broker",
    )


def _qty_to_broker_volume(qty: float, volume_per_qty: float = QTY_VOLUME_SCALE) -> int:
    """Convert qty units (risk-engine units) into broker volume units."""
    qty_value = coerce_positive_live_qty(qty)
    if not math.isfinite(float(volume_per_qty)) or float(volume_per_qty) <= 0:
        raise ValueError("volume_per_qty must be finite and positive")
    volume = int(round(qty_value * float(volume_per_qty)))
    if volume <= 0:
        raise ValueError(
            "Signal has no positive quantity ('qty'); "
            "refusing to submit an order with a default size."
        )
    return volume


def _broker_volume_to_qty(volume: float, volume_per_qty: float = QTY_VOLUME_SCALE) -> float:
    """Convert broker volume units into qty units (risk-engine units)."""
    return _to_float(volume, default=0.0) / float(volume_per_qty)


_NOT_AUTHORIZED_MARKER = "not authorized"

#: `str()` of the exception cTrader's client raises on a response timeout:
#: ``(15, 'Deferred')`` - see `_classify_deferred_response_timeout`.
_RESPONSE_TIMEOUT_SHAPE = re.compile(r"^\(\s*(\d+)\s*,\s*['\"]Deferred['\"]\s*\)$")


def _twisted_deferred_timeout_class() -> type[BaseException] | None:
    """`twisted.internet.defer.TimeoutError`, or None if Twisted cannot be imported.

    Imported lazily and by name: this module is loaded by the test suite and by deployments
    with no cTrader install, and a module-scope Twisted import would be a side effect this
    file otherwise does not have.
    """
    try:
        from twisted.internet import defer
    except Exception:  # pragma: no cover - Twisted is absent
        return None
    return getattr(defer, "TimeoutError", None)


def _classify_deferred_response_timeout(
    exc: BaseException, error_text: str
) -> tuple[bool, int | None]:
    """Is `exc` cTrader's *response* timeout, and how long did it wait?

    `(matched, seconds)`. The boolean is the classification; the seconds are best-effort and
    may be None, which is why they are not the return value on their own - a bare
    `defer.TimeoutError()` carries no duration and must still be classified, not dropped.

    **What this actually is.** `ctrader_open_api/client.py` does

        responseDeferred.addTimeout(responseTimeoutInSeconds, self._runningReactor)

    and Twisted's `Deferred.addTimeout` responds to expiry with

        raise TimeoutError(timeout, "Deferred")        # twisted/internet/defer.py

    so `args == (15, "Deferred")`, `str(exc) == "(15, 'Deferred')"`, and the 15 is
    `responseTimeoutInSeconds` (`_send_application_auth` passes `timeout_seconds=15`). It is
    a *response* timeout: the request was written to a socket that accepted it, and no reply
    arrived. It is therefore neither a credentials problem nor a TLS handshake failure.

    **The bug this fixes.** Twisted's `TimeoutError` is its own class deriving straight from
    `Exception` - it is *not* the builtin, and on 3.11+ the builtin is also
    `asyncio.TimeoutError`. So the pre-existing `isinstance(exc, asyncio.TimeoutError)`
    branch could never match a Twisted timeout; it was dead code for this exact failure. In
    production the error fell past it, past the `"token" in message` test, and into a
    *fallback* that named authentication as the cause - once per retry, forever, for
    credentials that were simultaneously authenticating in 1.0s from a fresh process.

    Two signals, because either alone is incomplete. The class is authoritative when
    Twisted is importable; the `str()` shape covers the case where it is not, and is
    deliberately narrow - anchored at both ends, and requiring the literal `Deferred`, so a
    merely-similar message or any other parenthesised pair falls through to the other
    branches. An over-strict guard is still a wrong guard, so whitespace inside the tuple is
    tolerated.
    """
    seconds: int | None = None
    match = _RESPONSE_TIMEOUT_SHAPE.match(str(error_text).strip())
    if match:
        seconds = int(match.group(1))

    timeout_class = _twisted_deferred_timeout_class()
    if timeout_class is None or not isinstance(exc, timeout_class):
        # Either not a Twisted timeout, or Twisted is unavailable and the string is all
        # there is. Only a full shape match may claim it - the weak signal must not carry
        # the classification on its own.
        return bool(match), seconds

    if seconds is None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int) and not isinstance(args[0], bool):
            seconds = int(args[0])
    return True, seconds
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


@contextmanager
def _exclusive_file_lock(handle: Any) -> Iterator[None]:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\0")
            handle.flush()
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_env_keys(path: Path, updates: dict[str, str]) -> None:
    """Persist dotenv keys atomically without logging secret material."""
    resolved = path.resolve()
    if not path.parent.is_dir():
        raise FileNotFoundError(
            f"Accounts env target must live in an existing directory: {path}"
        )
    target = Path(resolved)
    lock_path = target.with_name(f"{target.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        with _exclusive_file_lock(lock_handle):
            lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
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

            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            tmp_path = Path(tmp_name)
            try:
                tmp_handle = os.fdopen(tmp_fd, "w", encoding="utf-8")
                tmp_fd = -1
                with tmp_handle:
                    tmp_handle.write("\n".join(lines) + "\n")
                tmp_path.chmod(0o600)
                os.replace(tmp_path, target)
            finally:
                if tmp_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(tmp_fd)
                tmp_path.unlink(missing_ok=True)


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
        env_values: Mapping[str, str] | None = None,
    ):
        # C1: file-first credential precedence (see credential_env).
        self.app_id = app_id or resolve_env_value(env_values, "CTRADER_APP_ID")
        self.app_secret = app_secret or resolve_env_value(env_values, "CTRADER_APP_SECRET")
        self.access_token = access_token or resolve_env_value(env_values, "CTRADER_ACCESS_TOKEN")
        self.refresh_token = refresh_token or resolve_env_value(env_values, "CTRADER_REFRESH_TOKEN")
        self.accounts_env_path = (
            Path(accounts_env_path) if accounts_env_path else DEFAULT_ACCOUNTS_ENV
        )
        raw_account_id = str(account_id or account_config_id).strip()
        self.ctid_trader_account_id = int(raw_account_id) if raw_account_id else 0
        # Presence is checked separately from the value on purpose.
        # `resolve_env_value` returns "" both for a key that is absent and for one an
        # operator deliberately blanked, and it is built to keep those two apart -
        # "present-but-empty stays empty". Passing its result through `or None`
        # collapsed the blanked case into "absent", so the strict parse never saw the
        # empty string and applied the default instead. preflight and daemon both
        # refuse on `CTRADER_DEMO=""`; this is the reader that opens the connection,
        # and with `default=False` the same blank would have selected live trading
        # silently. So: absent means the default, present-but-blank is an error.
        demo_configured = (env_values is not None and "CTRADER_DEMO" in env_values) or (
            "CTRADER_DEMO" in os.environ
        )
        self.demo = (
            _parse_bool_env(
                "CTRADER_DEMO",
                resolve_env_value(env_values, "CTRADER_DEMO") if demo_configured else None,
                default=True,
            )
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
        self._execution_retry_counts: dict[int, int] = {}
        self._unresolved_execution_events: deque[Any] = deque()
        self._reconciliation_required = False
        self._reconciliation_retry_at = 0.0
        self._connected = False
        self._authenticated = False
        self._connect_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._symbol_name_to_id: dict[str, int] = {}
        # #80: broker-authoritative per-symbol scales (digits/lotSize), keyed
        # by upper symbol name. Tests/fallbacks use _fallback_symbol_scale.
        self._symbol_props: dict[str, SymbolScale] = {}
        self._position_ids_by_symbol: dict[str, tuple[int, ...]] = {}
        self._order_id_to_signal_id: dict[int, str] = {}
        # #80: order id -> upper symbol name (for order-only fills whose
        # payload carries no symbolId).
        self._order_id_to_symbol: dict[int, str] = {}
        self._terminal_auth_failure_reason = ""
        self._terminal_auth_failure_code = ""
        self._already_logged_in_notified = False
        self._refresh_flow = False

        self._client_class: Any = None
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
        self._ProtoOACancelOrderReq: Any = None
        self._ProtoOAClosePositionReq: Any = None
        self._ProtoOAExecutionEvent: Any = None
        self._ProtoOAOrderErrorEvent: Any = None
        self._ProtoOAErrorRes: Any = None
        self._ProtoOATraderReq: Any = None
        self._ProtoOATraderRes: Any = None
        self._ProtoOAOrderDetailsReq: Any = None
        self._ProtoOAOrderDetailsRes: Any = None
        self._ProtoOAOrderListReq: Any = None
        self._ProtoOAOrderListRes: Any = None
        self._ProtoOAOrderListByPositionIdReq: Any = None
        self._ProtoOAOrderListByPositionIdRes: Any = None
        self._ProtoOAReconcileReq: Any = None
        self._ProtoOAReconcileRes: Any = None
        self._ProtoOARefreshTokenReq: Any = None
        self._ProtoOARefreshTokenRes: Any = None
        self._ProtoOADealStatus: Any = None
        self._ProtoOAPositionStatus: Any = None
        self._ProtoOAOrderType: Any = None
        self._ProtoOAOrderStatus: Any = None
        self._ProtoOATradeSide: Any = None
        self._ProtoOAExecutionType: Any = None
        self._ProtoOATimeInForce: Any = None

        self._heartbeat_payload_type: int | None = None
        self._execution_payload_type: int | None = None
        self._error_payload_type: int | None = None
        self._order_error_payload_type: int | None = None

    def _load_openapi_types(self) -> None:
        if self._client_class is not None:
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
                ProtoOACancelOrderReq,
                ProtoOAClosePositionReq,
                ProtoOAErrorRes,
                ProtoOAExecutionEvent,
                ProtoOANewOrderReq,
                ProtoOAOrderDetailsReq,
                ProtoOAOrderDetailsRes,
                ProtoOAOrderErrorEvent,
                ProtoOAOrderListByPositionIdReq,
                ProtoOAOrderListByPositionIdRes,
                ProtoOAOrderListReq,
                ProtoOAOrderListRes,
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
                ProtoOAOrderStatus,
                ProtoOAOrderType,
                ProtoOAPositionStatus,
                ProtoOATimeInForce,
                ProtoOATradeSide,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ctrader-open-api package is required for CTraderLiveAdapter."
            ) from exc

        self._client_class = Client
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
        self._ProtoOACancelOrderReq = ProtoOACancelOrderReq
        self._ProtoOAClosePositionReq = ProtoOAClosePositionReq
        self._ProtoOAExecutionEvent = ProtoOAExecutionEvent
        self._ProtoOAOrderErrorEvent = ProtoOAOrderErrorEvent
        self._ProtoOAErrorRes = ProtoOAErrorRes
        self._ProtoOATraderReq = ProtoOATraderReq
        self._ProtoOATraderRes = ProtoOATraderRes
        self._ProtoOAOrderDetailsReq = ProtoOAOrderDetailsReq
        self._ProtoOAOrderDetailsRes = ProtoOAOrderDetailsRes
        self._ProtoOAOrderListReq = ProtoOAOrderListReq
        self._ProtoOAOrderListRes = ProtoOAOrderListRes
        self._ProtoOAOrderListByPositionIdReq = ProtoOAOrderListByPositionIdReq
        self._ProtoOAOrderListByPositionIdRes = ProtoOAOrderListByPositionIdRes
        self._ProtoOAReconcileReq = ProtoOAReconcileReq
        self._ProtoOAReconcileRes = ProtoOAReconcileRes
        self._ProtoOARefreshTokenReq = ProtoOARefreshTokenReq
        self._ProtoOARefreshTokenRes = ProtoOARefreshTokenRes
        self._ProtoOADealStatus = ProtoOADealStatus
        self._ProtoOAPositionStatus = ProtoOAPositionStatus
        self._ProtoOAOrderType = ProtoOAOrderType
        self._ProtoOAOrderStatus = ProtoOAOrderStatus
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
            # The accounts file is the source of truth (file-first precedence):
            # refreshed tokens are persisted above and must NOT be mirrored into
            # os.environ, where a stale copy would shadow the fresh file value
            # for any component resolving credentials through the file.
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
        # A connect event describes a socket that has not been authenticated yet, so
        # the per-connection auth state starts clean here. Disconnect and teardown
        # both clear these flags already; what neither covers is a transport that
        # reconnects without reporting the disconnect. Leaving `_authenticated` True
        # would let `_authenticate` return before sending a request, and every
        # `not self._authenticated` gate would then pass on a connection the broker
        # never authenticated.
        self._authenticated = False
        self._already_logged_in_notified = False
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
            self._handle_error_response_payload(payload)
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

    def _handle_error_response_payload(self, payload: Any) -> None:
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
                if (
                    self._ProtoOAAccountAuthReq is not None
                    and self._ProtoOAAccountAuthRes is not None
                ):
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
        await self._send_application_auth()
        await self._send_account_auth_with_recovery()
        await self._load_symbols_into_cache()

    async def _send_application_auth(self) -> None:
        app_auth_req = self._ProtoOAApplicationAuthReq()
        app_auth_req.clientId = self.app_id
        app_auth_req.clientSecret = self.app_secret
        app_auth_res = await self._send_request(app_auth_req, timeout_seconds=15)
        if not isinstance(app_auth_res, self._ProtoOAApplicationAuthRes):
            raise RuntimeError(
                f"Unexpected application auth response: {type(app_auth_res).__name__}"
            )

    async def _send_account_auth_with_recovery(self) -> None:
        try:
            account_auth_res = await self._send_account_auth_request(timeout_seconds=15)
        except CTraderApiError as exc:
            await self._recover_account_auth_failure(exc)
        else:
            if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
                raise RuntimeError(
                    f"Unexpected account auth response: {type(account_auth_res).__name__}"
                )

    async def _recover_account_auth_failure(self, exc: CTraderApiError) -> None:
        error_code = _normalize_error_code(exc.error_code)
        error_text = str(exc)
        if _is_already_logged_in_error(error_code, error_text):
            logger.info(
                "cTrader account already authenticated; reusing current session",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            return
        if not _is_terminal_auth_error(error_code, error_text):
            raise exc
        if not await self._refresh_access_token():
            reason = exc.description or error_text or _CTRADER_AUTH_FAILURE_FALLBACK
            self._mark_terminal_auth_failure(reason, error_code=error_code)
            raise CTraderAuthTerminalError(reason, error_code=error_code) from exc
        account_auth_res = await self._send_account_auth_request(timeout_seconds=15)
        if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
            raise RuntimeError(
                "Unexpected account auth response after refresh: "
                f"{type(account_auth_res).__name__}"
            )

    def _scale_for(self, symbol: str) -> SymbolScale:
        """Return the per-symbol broker scale, failing closed (#80).

        Broker-cached properties win; known classes may use the static table;
        unknown symbols without broker properties raise instead of guessing.
        """
        upper = str(symbol or "").strip().upper()
        cached = self._symbol_props.get(upper)
        if cached is not None:
            return cached
        return _fallback_symbol_scale(upper)

    def _position_id_for_exit(
        self, symbol: str, requested_position_id: int | None = None
    ) -> int:
        upper = str(symbol or "").strip().upper()
        position_ids = self._position_ids_by_symbol.get(upper, ())
        if requested_position_id is not None:
            if requested_position_id <= 0:
                raise ValueError(
                    f"cTrader exit-only for {upper} has an invalid broker position id."
                )
            if not position_ids:
                raise ValueError(
                    f"cTrader exit-only for {upper} has no broker position id; "
                    "refusing to submit an unaddressed close."
                )
            if requested_position_id not in position_ids:
                raise ValueError(
                    f"cTrader exit-only position id {requested_position_id} is not the "
                    f"current broker position for {upper}."
                )
            return requested_position_id
        if len(position_ids) > 1:
            raise ValueError(
                f"cTrader exit-only for {upper} has multiple broker positions; "
                "a single position-addressed close cannot represent the book."
            )
        if not position_ids:
            raise ValueError(
                f"cTrader exit-only for {upper} has no broker position id; "
                "refusing to submit an unaddressed close."
            )
        return position_ids[0]

    def _requested_exit_position_id(self, metadata: Mapping[str, Any]) -> int | None:
        raw_position_id = metadata.get("position_id")
        raw_position_ids = metadata.get("position_ids")
        if raw_position_ids is not None:
            if not isinstance(raw_position_ids, (list, tuple)) or len(raw_position_ids) != 1:
                raise ValueError(
                    "cTrader exit-only requires exactly one broker position id."
                )
            raw_position_id = raw_position_ids[0]
        if raw_position_id is None:
            return None
        if isinstance(raw_position_id, bool):
            raise ValueError("cTrader exit-only position id is not an integer.")
        try:
            position_id = int(raw_position_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("cTrader exit-only position id is not an integer.") from exc
        if isinstance(raw_position_id, float) and raw_position_id != position_id:
            raise ValueError("cTrader exit-only position id is not an integer.")
        if position_id <= 0:
            raise ValueError("cTrader exit-only position id must be positive.")
        return position_id

    def _symbol_name_for_id(self, symbol_id: int) -> str:
        """Resolve a broker symbol id to its upper name, failing closed (#80).

        A fill/position for an unmapped id cannot be scaled — guessing FX
        would misstate exposure by 10-1000x on XAU/indices.
        """
        for name, sid in self._symbol_name_to_id.items():
            if sid == int(symbol_id):
                return name
        raise ValueError(
            f"cTrader symbol id {symbol_id!r} is not in the symbols cache; "
            "refusing to scale its volume with a guessed symbol."
        )

    async def _load_symbols_into_cache(self) -> None:
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
        # #80: broker-API symbol properties (digits/lotSize/minVolume) are the
        # source of truth for per-symbol scales. Best-effort: the light list
        # above stays authoritative for ids when the detail fetch fails.
        await self._load_symbol_props_into_cache(symbol_name_to_id)
        self._authenticated = True

    async def tradeable_symbol_names(self) -> set[str]:
        """Upper-case names of the instruments *this account* can trade.

        The symbols list is broker-returned per cTID, so this is the account's
        own contract identity rather than a global symbol table: an account
        without XAUUSD genuinely does not have it, and no amount of local
        configuration changes that. Used to tell an operator at arming time
        that a deployment is configured with instruments the account cannot
        trade, instead of at submit time when the first signal is rejected.
        """
        if not self._authenticated or not self._symbol_name_to_id:
            await self._authenticate()
        return set(self._symbol_name_to_id)

    async def _load_symbol_props_into_cache(self, symbol_name_to_id: dict[str, int]) -> None:
        """Fetch full ProtoOASymbol properties per symbol id (#80).

        ProtoOALightSymbol (SymbolsList) exposes no digits/lotSize — those
        live on ProtoOASymbol via ProtoOASymbolByIdReq. Verified against the
        installed ctrader_open_api descriptors before touching this path:
        digits/pipPosition required, lotSize/min/max/stepVolume optional.
        """
        if not symbol_name_to_id:
            self._symbol_props.clear()
            return
        for name in symbol_name_to_id:
            self._symbol_props.pop(name, None)
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOASymbolByIdReq,
                ProtoOASymbolByIdRes,
            )
        except (ModuleNotFoundError, ImportError):
            return
        id_to_name = {sid: name for name, sid in symbol_name_to_id.items()}
        try:
            detail_req = ProtoOASymbolByIdReq()
            detail_req.ctidTraderAccountId = self.ctid_trader_account_id
            detail_req.symbolId.extend(sorted(id_to_name))
            detail_res = await self._send_request_with_refresh_retry(
                detail_req, timeout_seconds=15
            )
        except Exception:
            logger.warning(
                "cTrader symbol-props fetch failed; using fallback scales",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            return
        if not isinstance(detail_res, ProtoOASymbolByIdRes):
            return
        # Archived entries first so live properties always win on collision.
        for detail in list(getattr(detail_res, "archivedSymbol", [])) + list(detail_res.symbol):
            try:
                sid = int(detail.symbolId)
            except (TypeError, ValueError):
                continue
            name = id_to_name.get(sid)
            if name is None:
                continue
            fallback = _fallback_symbol_scale(name) if _fallback_scale_class(name) else None
            scale = _symbol_scale_from_broker_props(
                digits=getattr(detail, "digits", None),
                lot_size=getattr(detail, "lotSize", None),
                min_volume=getattr(detail, "minVolume", None),
                fallback=fallback,
            )
            if scale is not None:
                self._symbol_props[name] = scale

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
        """Resolve broker volume from the binding risk-engine ``qty``."""
        scale = self._scale_for(signal.symbol)
        metadata = signal.metadata or {}
        raw_keys = [key for key in ("ctrader_volume", "volume") if key in metadata]
        if raw_keys:
            raise ValueError(
                "Raw cTrader volume overrides "
                f"({', '.join(raw_keys)}) are ambiguous; use the binding 'qty' "
                "passed by the daemon."
            )
        if "qty" not in metadata:
            raise ValueError(
                "Signal has no positive quantity ('qty'); refusing to submit "
                "an order with a default size."
            )
        try:
            qty = coerce_positive_live_qty(metadata.get("qty"))
        except ValueError as exc:
            raise ValueError(
                "Signal has no positive quantity ('qty'); refusing to "
                "submit an order with a default size."
            ) from exc
        volume = _qty_to_broker_volume(qty, scale.volume_per_qty)
        if volume < scale.min_volume:
            raise ValueError(
                f"Risk-sized volume {volume} for {signal.symbol} is below "
                f"the broker minimum ({scale.min_volume}); order rejected "
                "instead of silently upsizing beyond the risk limit."
            )
        return volume

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
            max_attempts, base_delay, connect_timeout = self._connect_retry_parameters()
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
                    await self._reconcile_after_connect()
                    return
                except asyncio.CancelledError:
                    self._teardown_client()
                    raise
                except CTraderAuthTerminalError as exc:
                    self._handle_terminal_auth_failure(exc)
                    raise
                except Exception as exc:
                    await self._retry_or_raise_connect_failure(
                        exc, attempt, max_attempts, base_delay
                    )

    def _connect_retry_parameters(self) -> tuple[int, float, float]:
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
        return max_attempts, base_delay, connect_timeout

    async def _reconcile_after_connect(self) -> None:
        try:
            await self.reconcile_executions()
        except CTraderAuthTerminalError:
            raise
        except Exception as exc:
            self._reconciliation_required = True
            self._reconciliation_retry_at = 0.0
            logger.warning(
                "cTrader execution reconciliation failed; retry is pending",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error": str(exc),
                    }
                },
            )

    def _handle_terminal_auth_failure(self, exc: CTraderAuthTerminalError) -> None:
        reason = str(exc).strip() or "cTrader authorization failed"
        self._mark_terminal_auth_failure(
            reason,
            error_code=getattr(exc, "error_code", ""),
        )
        self._teardown_client()
        self._log_terminal_auth_failure(
            reason, error_code=getattr(exc, "error_code", "")
        )

    def _log_terminal_auth_failure(
        self, reason: str, *, error_code: str
    ) -> None:
        if _NOT_AUTHORIZED_MARKER in reason.lower():
            logger.warning(
                "Account %s not authorized for token, skipping",
                self.ctid_trader_account_id,
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "account_config_id": self.account_config_id,
                        "error_code": error_code,
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
                        "error_code": error_code,
                        "error": reason,
                    }
                },
            )

    async def _retry_or_raise_connect_failure(
        self,
        exc: Exception,
        attempt: int,
        max_attempts: int,
        base_delay: float,
    ) -> None:
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
            return
        self._log_connect_failure(exc, error_text, message)
        raise

    def _log_connect_failure(
        self, exc: Exception, error_text: str, message: str
    ) -> None:
        # A response timeout is checked FIRST, and as its own case, because it is
        # categorically not a credentials problem: the socket is up and the request was
        # written to it. Routing it into the token branch is how an operator ends up
        # refreshing a credential that works - demonstrated in production, where the same
        # credentials authenticated in 1.0s from a fresh process while the long-lived daemon
        # timed out at 15s with this exact text.
        is_response_timeout, response_timeout_s = _classify_deferred_response_timeout(
            exc, error_text
        )
        if is_response_timeout:
            waited = (
                f"within {response_timeout_s}s"
                if response_timeout_s is not None
                else "within the configured response timeout"
            )
            logger.info(
                "cTrader request written but NO RESPONSE %s; the transport is up, so this "
                "is not a credentials problem and not a TLS handshake failure. The request "
                "reached the socket and nothing came back.",
                waited,
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error": error_text,
                        "response_timeout_s": response_timeout_s,
                    }
                },
            )
        elif "token" in message or "auth" in message:
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
            # **This message used to say "connect/auth unavailable".** That is a fallback: it
            # fires precisely when the cause could not be classified, and then names a cause
            # anyway. In production it printed once per retry, forever, for a transport that
            # was demonstrably fine, and every one of those lines pointed an operator at a
            # token that worked. A fallback that guesses is worse than one that says so. The
            # `error` field carries the real text, so nothing is lost by pointing at it.
            logger.info(
                "cTrader connect failed; cause NOT CLASSIFIED - read the error field rather "
                "than assuming credentials. daemon will retry",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error": error_text,
                    }
                },
            )

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
        client = self._client_class(host, self._EndPoints.PROTOBUF_PORT, self._TcpProtocol)
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
        *,
        position_id: int | None = None,
    ) -> str | None:
        """Find exactly one account-scoped order for a persisted signal intent."""
        if signal_timestamp is None:
            return None
        if signal_timestamp.tzinfo is None:
            signal_timestamp = signal_timestamp.replace(tzinfo=timezone.utc)
        signal_timestamp = signal_timestamp.astimezone(timezone.utc)
        from_timestamp = int(
            (signal_timestamp - timedelta(minutes=15)).timestamp() * 1000
        )
        to_timestamp = int(
            (signal_timestamp + timedelta(minutes=15)).timestamp() * 1000
        )

        if position_id is None:
            request_type = self._ProtoOAOrderListReq
            expected_response_type = self._ProtoOAOrderListRes
            if request_type is None or expected_response_type is None:
                return None
            request = request_type()
            request.ctidTraderAccountId = self.ctid_trader_account_id
            request.fromTimestamp = from_timestamp
            request.toTimestamp = to_timestamp
        else:
            request_type = self._ProtoOAOrderListByPositionIdReq
            expected_response_type = self._ProtoOAOrderListByPositionIdRes
            if request_type is None or expected_response_type is None:
                return None
            request = request_type()
            request.ctidTraderAccountId = self.ctid_trader_account_id
            request.positionId = position_id
            request.fromTimestamp = from_timestamp
            request.toTimestamp = to_timestamp
        response = await self._send_request_with_refresh_retry(
            request, timeout_seconds=15
        )
        if not isinstance(response, expected_response_type):
            return None
        if bool(getattr(response, "hasMore", False)):
            logger.warning(
                "cTrader recovery order window is incomplete; refusing ambiguous match",
                extra={"data": {"signal_id": signal_id}},
            )
            return None
        if position_id is None:
            matches = [
                order
                for order in getattr(response, "order", [])
                if str(getattr(order, "clientOrderId", "") or "").strip() == signal_id
                and int(getattr(order, "orderId", 0) or 0) > 0
            ]
        else:
            matches = [
                order
                for order in getattr(response, "order", [])
                if int(getattr(order, "positionId", 0) or 0) == position_id
                and bool(getattr(order, "closingOrder", False))
                and int(getattr(order, "orderId", 0) or 0) > 0
            ]
        if len(matches) != 1:
            if len(matches) > 1:
                logger.error(
                    "Multiple cTrader orders matched one signal; refusing recovery",
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "position_id": position_id,
                            "match_count": len(matches),
                        }
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
        signal_ids = await self._list_reconciliation_signal_ids(max_signals)
        recovered = 0
        for signal_id in signal_ids:
            signal_uuid = self._parse_signal_uuid(signal_id)
            if signal_uuid is None:
                continue
            broker_order_id = await asyncio.to_thread(
                get_live_signal_broker_order_id, signal_id
            )
            if not broker_order_id:
                broker_order_id = await self._recover_and_persist_broker_order_id(signal_id)
                if not broker_order_id:
                    continue
            order_id = self._coerce_broker_order_id(broker_order_id)
            if order_id is None:
                continue
            details_res = await self._fetch_reconciliation_order_details(
                signal_id=signal_id,
                broker_order_id=broker_order_id,
                order_id=order_id,
            )
            if details_res is None:
                continue
            recovered += await self._persist_recovered_deals(
                signal_id=signal_id,
                signal_uuid=signal_uuid,
                broker_order_id=broker_order_id,
                details_res=details_res,
            )
        return recovered

    def _parse_signal_uuid(self, signal_id: str) -> UUID | None:
        try:
            return UUID(signal_id)
        except ValueError:
            return None

    def _coerce_broker_order_id(self, broker_order_id: str) -> int | None:
        if isinstance(broker_order_id, bool):
            return None
        try:
            order_id = int(broker_order_id)
        except (TypeError, ValueError):
            return None
        return order_id if order_id > 0 else None

    async def _list_reconciliation_signal_ids(self, max_signals: int) -> list[str]:
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
        return signal_ids

    async def _persisted_exit_position_id(self, signal_id: str) -> int | None:
        metadata = await asyncio.to_thread(get_live_signal_metadata, signal_id)
        if not metadata:
            return None
        if str(metadata.get("risk_sizing_mode") or "") != EXIT_ONLY_RISK_SIZING_MODE:
            return None
        return self._requested_exit_position_id(metadata)

    async def _recover_and_persist_broker_order_id(self, signal_id: str) -> str | None:
        signal_timestamp = await asyncio.to_thread(
            get_live_signal_timestamp, signal_id
        )
        position_id = await self._persisted_exit_position_id(signal_id)
        broker_order_id = await self._recover_broker_order_id(
            signal_id, signal_timestamp, position_id=position_id
        )
        if not broker_order_id:
            return None
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id, signal_id, broker_order_id
        )
        if not persisted:
            logger.error(
                "Recovered cTrader order but could not persist broker order id",
                extra={"data": {"signal_id": signal_id}},
            )
            return None
        self._order_id_to_signal_id[int(broker_order_id)] = signal_id
        return broker_order_id

    async def _fetch_reconciliation_order_details(
        self,
        *,
        signal_id: str,
        broker_order_id: str,
        order_id: int,
    ) -> Any | None:
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
            return None
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
            return None
        return details_res

    def _build_reconciled_execution(
        self,
        deal: Any,
        *,
        signal_uuid: UUID,
        broker_order_id: str,
        order: Any | None = None,
    ) -> LiveExecution | None:
        deal_status = int(getattr(deal, "dealStatus", 0) or 0)
        if deal_status not in {
            self._ProtoOADealStatus.FILLED,
            self._ProtoOADealStatus.PARTIALLY_FILLED,
        }:
            return None
        deal_id = int(getattr(deal, "dealId", 0) or 0)
        if deal_id <= 0:
            return None
        filled_volume = self._resolve_filled_volume(order=order, deal=deal)
        if filled_volume <= 0:
            return None
        # Unit contract: persist filled_qty in qty units (broker volume per
        # the symbol scale, #80), the same unit the risk engine sized and
        # the viewer consumes.
        deal_symbol_id = int(getattr(deal, "symbolId", 0) or 0)
        if not deal_symbol_id and order is not None:
            deal_symbol_id = int(getattr(order, "symbolId", 0) or 0)
        deal_symbol = self._symbol_name_for_id(deal_symbol_id) if deal_symbol_id else None
        if deal_symbol is None:
            try:
                deal_symbol = self._order_id_to_symbol.get(int(broker_order_id))
            except (TypeError, ValueError):
                deal_symbol = None
        if deal_symbol is None:
            raise ValueError(
                "cTrader deal carries no symbolId; refusing to scale its "
                "volume with a guessed symbol."
            )
        filled_qty = _broker_volume_to_qty(
            filled_volume, self._scale_for(deal_symbol).volume_per_qty
        )

        timestamp_ms = 0
        if int(getattr(deal, "executionTimestamp", 0) or 0) > 0:
            timestamp_ms = int(deal.executionTimestamp)
        elif int(getattr(deal, "utcLastUpdateTimestamp", 0) or 0) > 0:
            timestamp_ms = int(deal.utcLastUpdateTimestamp)

        return LiveExecution(
            signal_id=signal_uuid,
            actual_price=_to_float(getattr(deal, "executionPrice", 0)),
            filled_qty=filled_qty,
            commission=_to_float(getattr(deal, "commission", 0)),
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=broker_order_id,
            dedupe_key=f"ctrader:deal:{deal_id}",
            # This path returns early when `deal_id <= 0`, so it is positive here.
            broker_fill_id=deal_id,
        )

    async def _persist_recovered_deals(
        self,
        *,
        signal_id: str,
        signal_uuid: UUID,
        broker_order_id: str,
        details_res: Any,
    ) -> int:
        recovered = 0
        details_order = getattr(details_res, "order", None)
        if isinstance(details_order, (list, tuple)):
            details_order = details_order[0] if details_order else None
        for deal in getattr(details_res, "deal", []):
            execution = self._build_reconciled_execution(
                deal,
                signal_uuid=signal_uuid,
                broker_order_id=broker_order_id,
                order=details_order,
            )
            if execution is None:
                continue
            if await asyncio.to_thread(save_live_execution, execution):
                recovered += 1
                logger.info(
                    "Reconciled missed cTrader fill",
                    extra={
                        "data": {
                            "signal_id": signal_id,
                            "broker_order_id": broker_order_id,
                            "deal_id": int(getattr(deal, "dealId", 0) or 0),
                            "filled_qty": execution.filled_qty,
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
        self._position_ids_by_symbol.clear()
        # #80: props and order-symbol mappings are session state — a
        # reconnect must re-fetch broker props, never trade on stale scales,
        # and the order map must not grow unboundedly across sessions.
        self._symbol_props.clear()
        self._order_id_to_symbol.clear()
        self._execution_queue = asyncio.Queue(maxsize=5000)
        self._execution_retry_counts.clear()
        self._unresolved_execution_events.clear()
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

    def _validate_pre_persistence(self, signal: LiveSignal) -> None:
        """B-8: validate symbol/action BEFORE the first persistence.

        A signal persisted without a broker order wedges every redelivery on
        the duplicate-persisted recovery path, so the cheap in-memory checks
        run ahead of save_live_signal.
        """
        if signal.action not in {"buy", "sell"}:
            raise ValueError(f"Unsupported action '{signal.action}'")
        if self._symbol_name_to_id.get(signal.symbol.upper()) is None:
            raise ValueError(
                f"Unknown cTrader symbol: {signal.symbol!r}; refusing to "
                "persist or submit the signal."
            )

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self._raise_if_terminal_auth_failed()
        signal_id = str(signal.id)
        existing_order_id = await asyncio.to_thread(
            get_live_signal_broker_order_id, signal_id
        )
        if existing_order_id:
            normalized_order_id = self._coerce_broker_order_id(existing_order_id)
            if normalized_order_id is None:
                raise RuntimeError(
                    "Persisted cTrader broker order id is invalid; refusing duplicate submission."
                )
            self._order_id_to_signal_id[normalized_order_id] = signal_id
            return str(normalized_order_id)

        if not self._connected or not self._authenticated:
            await self.connect()

        self._validate_pre_persistence(signal)

        # C1/B2: size exactly once upstream (deployment RiskConfig); the
        # incoming qty is binding. No default-config sizing here.
        if qty is None:
            raise ValueError(
                "cTrader submit_order requires an explicit risk-sized qty; "
                "size exactly once upstream and pass it as binding "
                f"(signal_id={signal.id}, symbol={signal.symbol})."
            )
        try:
            qty_value = coerce_positive_live_qty(qty, field_name="cTrader order qty")
        except ValueError as exc:
            raise ValueError(
                "cTrader order qty must be finite and positive, "
                f"got {qty!r} (signal_id={signal.id}, symbol={signal.symbol})."
            ) from exc
        signal = signal.model_copy(
            update={
                "metadata": {
                    **(signal.metadata or {}),
                    "qty": qty_value,
                    "explicit_qty": qty_value,
                }
            }
        )
        exit_only = is_exit_only_metadata(signal.metadata)
        self._resolve_order_volume(signal)
        signal = signal.model_copy(
            update={
                "metadata": {
                    key: value
                    for key, value in (signal.metadata or {}).items()
                    if key not in {"ctrader_volume", "volume"}
                }
            }
        )
        persisted_timestamp = await asyncio.to_thread(
            get_live_signal_timestamp, signal_id
        )
        if persisted_timestamp is not None:
            return await self._recover_persisted_signal_order_id(
                signal_id,
                signal_timestamp=persisted_timestamp,
                unrecovered_error=(
                    "Persisted cTrader signal has no unique recoverable broker order; "
                    "refusing possible duplicate submission."
                ),
                unpersisted_error=(
                    "Recovered cTrader order but failed to persist broker order id."
                ),
            )

        position_id: int | None = None
        if exit_only:
            requested_position_id = self._requested_exit_position_id(signal.metadata or {})
            position_id = self._position_id_for_exit(signal.symbol, requested_position_id)
            signal = signal.model_copy(
                update={
                    "metadata": {
                        **(signal.metadata or {}),
                        "position_id": position_id,
                    }
                }
            )
            self._resolve_order_volume(signal)

        canonical_signal_id, created = await asyncio.to_thread(save_live_signal, signal)
        if not created:
            return await self._recover_duplicate_signal_order_id(canonical_signal_id)

        symbol_id = await self._resolve_symbol_id(signal.symbol)
        if exit_only:
            if position_id is None:
                raise RuntimeError("cTrader exit-only position id was not resolved.")
            request = self._build_close_position_request(
                position_id=position_id,
                volume=self._resolve_order_volume(signal),
            )
        else:
            request = self._build_new_order_request(signal, symbol_id=symbol_id)
        response = await self._submit_new_order_request(request)

        order_id = self._extract_order_id_from_payload(response)
        if order_id is None:
            # Fail closed (#77): an order without a usable broker id must not
            # surface as success-shaped "" — downstream would skip retry and
            # persistence, silently losing the order.
            raise RuntimeError(
                "cTrader order response carried no usable order id "
                f"(signal_id={signal.id}, symbol={signal.symbol}); the order "
                "cannot be tracked or retried safely."
            )
        broker_order_id = str(order_id)
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id, canonical_signal_id, broker_order_id
        )
        if not persisted:
            raise RuntimeError("Failed to persist cTrader broker order id.")
        self._order_id_to_signal_id[order_id] = canonical_signal_id
        # #80: remember the symbol for order-only fills (no symbolId).
        self._order_id_to_symbol[order_id] = str(signal.symbol).strip().upper()

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

    async def _recover_persisted_signal_order_id(
        self,
        signal_id: str,
        *,
        signal_timestamp: datetime | None,
        unrecovered_error: str,
        unpersisted_error: str,
    ) -> str:
        position_id = await self._persisted_exit_position_id(signal_id)
        recovered_order_id = await self._recover_broker_order_id(
            signal_id, signal_timestamp, position_id=position_id
        )
        if not recovered_order_id:
            raise RuntimeError(unrecovered_error)
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id, signal_id, recovered_order_id
        )
        if not persisted:
            raise RuntimeError(unpersisted_error)
        self._order_id_to_signal_id[int(recovered_order_id)] = signal_id
        return recovered_order_id

    async def _recover_duplicate_signal_order_id(self, canonical_signal_id: str) -> str:
        existing_order_id = await asyncio.to_thread(
            get_live_signal_broker_order_id, canonical_signal_id
        )
        if existing_order_id:
            normalized_order_id = self._coerce_broker_order_id(existing_order_id)
            if normalized_order_id is None:
                raise RuntimeError(
                    "Persisted cTrader broker order id is invalid; refusing duplicate submission."
                )
            self._order_id_to_signal_id[normalized_order_id] = canonical_signal_id
            return str(normalized_order_id)
        canonical_timestamp = await asyncio.to_thread(
            get_live_signal_timestamp, canonical_signal_id
        )
        return await self._recover_persisted_signal_order_id(
            canonical_signal_id,
            signal_timestamp=canonical_timestamp,
            unrecovered_error=(
                "Duplicate Discord signal has no unique recoverable broker order; "
                "refusing possible duplicate submission."
            ),
            unpersisted_error=(
                "Recovered duplicate Discord order but failed to persist broker order id."
            ),
        )

    async def _resolve_symbol_id(self, symbol: str) -> int | None:
        symbol_id = self._symbol_name_to_id.get(symbol.upper())
        if symbol_id is None:
            await self._authenticate()
            symbol_id = self._symbol_name_to_id.get(symbol.upper())
        if symbol_id is None:
            raise ValueError(
                f"Unknown cTrader symbol: {symbol!r}; refusing to "
                "persist or submit the signal."
            )
        return symbol_id

    def _build_close_position_request(self, *, position_id: int, volume: int) -> Any:
        if self._ProtoOAClosePositionReq is None:
            raise RuntimeError("cTrader close-position request type is unavailable.")
        if isinstance(position_id, bool):
            raise ValueError("cTrader close position id must be positive.")
        try:
            position_id_value = int(position_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("cTrader close position id must be positive integer.") from exc
        if position_id_value <= 0 or (
            isinstance(position_id, float) and position_id != position_id_value
        ):
            raise ValueError("cTrader close position id must be positive integer.")
        volume_value = coerce_positive_live_qty(volume, field_name="cTrader close volume")
        broker_volume = int(round(volume_value))
        if broker_volume <= 0:
            raise ValueError("cTrader close volume must be a positive integer.")
        request = self._ProtoOAClosePositionReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = position_id_value
        request.volume = broker_volume
        return request

    def _build_new_order_request(self, signal: LiveSignal, *, symbol_id: int) -> Any:
        if signal.action not in {"buy", "sell"}:
            raise ValueError(f"Unsupported signal action: {signal.action}")
        metadata = signal.metadata or {}
        if is_exit_only_metadata(metadata):
            # The exit-only decision rests on the system-stamped metadata, not
            # on the order side: the close side is DERIVED from the book (a
            # short must be bought back), so it is a consequence, never an
            # input. The invariant enforced here is therefore positional, not
            # directional -- an exit-only order is always a close addressed at a
            # broker position id, and this new-order path can never be one.
            raise ValueError(
                "cTrader exit-only orders must use a broker position-addressed close request."
            )
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
        # One definition, in shared/models.py, shared with live_runner/alpaca_adapter.py
        # and risk/live.py. These were inline literals here, so the alias list existed in
        # three places and two of them were spelled as a tuple in a call. See the note in
        # shared/models.py: if the paths drift, the same signal is bracketed differently
        # depending on which account it routed to.
        stop_loss = _first_metadata_float(metadata, SL_METADATA_KEYS)
        take_profit = _first_metadata_float(metadata, TP_METADATA_KEYS)
        unbounded_tp = take_profit is None and is_unbounded_tp(metadata)
        if stop_loss is None or (take_profit is None and not unbounded_tp):
            missing = [
                name
                for name, value in (("stop_loss", stop_loss), ("take_profit", take_profit))
                if value is None
            ]
            raise ValueError(
                "Refusing to submit cTrader order without "
                f"{' and '.join(missing)} (signal_id={signal.id}, "
                f"symbol={signal.symbol}); opening an unbounded position is not allowed."
            )
        relative_stop, relative_take = self._resolve_relative_sl_tp(
            signal, stop_loss, take_profit
        )
        request.relativeStopLoss = relative_stop
        request.relativeTakeProfit = relative_take
        return request

    def _resolve_relative_sl_tp(
        self, signal: LiveSignal, stop_loss: float, take_profit: float | None
    ) -> tuple[int, int]:
        """Convert absolute SL/TP prices into cTrader relative units.

        Per the cTrader Open API docs, ProtoOANewOrderReq's absolute
        stopLoss/takeProfit fields are "Not supported for MARKET orders" — this
        adapter submits MARKET/IMMEDIATE_OR_CANCEL orders, so the distances are
        sent via relativeStopLoss/relativeTakeProfit instead: int64 in
        1/100000 of a price unit (123000 means 1.23), where for BUY
        stopLoss = entryPrice - relativeStopLoss and takeProfit
        = entryPrice + relativeTakeProfit (reversed for SELL).
        """
        reference_price = float(signal.theoretical_price)
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError(
                f"Invalid reference price for {signal.symbol}: "
                f"{signal.theoretical_price!r}; refusing to submit."
            )
        self._scale_for(signal.symbol)
        scale = CTRADER_RELATIVE_PRICE_UNIT_DIVISOR

        def _distance(price: float) -> int:
            if math.isnan(price) or math.isinf(price) or price <= 0:
                raise ValueError(
                    f"Invalid SL/TP price for {signal.symbol}: {price!r}; "
                    "refusing to submit."
                )
            distance = abs(reference_price - price)
            if distance <= 0:
                raise ValueError(
                    f"SL/TP price {price!r} equals the reference price "
                    f"{reference_price!r} for {signal.symbol}; refusing to "
                    "submit a zero-distance SL/TP."
                )
            raw = distance * scale
            if not math.isfinite(raw) or raw < 1:
                raise ValueError(
                    f"SL/TP distance for {signal.symbol} resolves to {raw!r} "
                    f"(below one 1/{scale} price unit); refusing to submit."
                )
            # Distance is signed by direction at the protocol level; the value
            # sent is always the positive magnitude in 1/scale price units.
            return int(round(raw))

        buy = signal.action == "buy"
        stop_outward = reference_price - stop_loss if buy else stop_loss - reference_price
        if stop_outward <= 0:
            raise ValueError(
                f"SL price is on the wrong side of the reference price for "
                f"{signal.symbol} ({signal.action}): stop_loss={stop_loss!r}, "
                f"reference={reference_price!r}; refusing to submit."
            )
        if take_profit is None:
            # C1: explicit unbounded policy — stop-only order. cTrader treats
            # a zero relativeTakeProfit as "no take profit".
            return (_distance(stop_loss), 0)
        take_outward = take_profit - reference_price if buy else reference_price - take_profit
        if take_outward <= 0:
            raise ValueError(
                f"TP price is on the wrong side of the reference price for "
                f"{signal.symbol} ({signal.action}): take_profit={take_profit!r}, "
                f"reference={reference_price!r}; refusing to submit."
            )
        return (_distance(stop_loss), _distance(take_profit))

    async def _submit_new_order_request(self, request: Any) -> Any:
        try:
            response = await self._send_request_with_refresh_retry(request, timeout_seconds=20)
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if isinstance(response, self._ProtoOAOrderErrorEvent):
            raise RuntimeError(
                f"Order rejected ({response.errorCode}): {response.description or 'no description'}"
            )
        return response

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
        money_digits = self._resolve_money_digits(trader, context="account refresh")
        scale = 10**money_digits if 0 <= money_digits <= 12 else 100
        balance = _to_float(getattr(trader, "balance", 0)) / scale
        equity = balance
        if hasattr(trader, "equity"):
            equity = _to_float(getattr(trader, "equity", balance)) / scale
        return balance, equity

    @property
    def supports_cancel_orders(self) -> bool:
        return True

    @property
    def supports_flatten(self) -> bool:
        return True

    async def _fetch_open_positions(self) -> list[Any]:
        """Return raw open-position protos, shared by get_positions and flatten."""
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()
        self._raise_if_terminal_auth_failed()

        reconcile_req = self._ProtoOAReconcileReq()
        reconcile_req.ctidTraderAccountId = self.ctid_trader_account_id
        try:
            reconcile_res = await self._send_request_with_refresh_retry(
                reconcile_req, timeout_seconds=15
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if not isinstance(reconcile_res, self._ProtoOAReconcileRes):
            raise RuntimeError(
                f"Unexpected reconcile response: {type(reconcile_res).__name__}"
            )

        open_positions: list[Any] = []
        for position in getattr(reconcile_res, "position", []):
            status = int(getattr(position, "positionStatus", 0) or 0)
            if status != self._ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            if getattr(position, "tradeData", None) is None:
                continue
            open_positions.append(position)
        return open_positions

    async def _reconcile_state(self) -> Any:
        """Return the current account state (orders + positions) from one call.

        Reconcile, not OrderList. OrderList is a historical query bounded by
        from/toTimestamp; 0/0 does not mean "now", so it can answer with
        nothing (and a cancel that finds nothing reports success while leaving
        the order resting) or with unrelated history. Reconcile takes no time
        bounds and is by definition current state, which is the same call
        _fetch_open_positions already makes for positions.

        Shared by the blanket cancel and the orphan sweep so both see one
        consistent snapshot: a sweep that read positions and orders from two
        different snapshots could cancel a protection for a position that had
        just been closed *and* re-opened.
        """
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()
        self._raise_if_terminal_auth_failed()

        reconcile_req = self._ProtoOAReconcileReq()
        reconcile_req.ctidTraderAccountId = self.ctid_trader_account_id
        try:
            reconcile_res = await self._send_request_with_refresh_retry(
                reconcile_req, timeout_seconds=15
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if not isinstance(reconcile_res, self._ProtoOAReconcileRes):
            raise RuntimeError(
                f"Unexpected reconcile response: {type(reconcile_res).__name__}"
            )
        return reconcile_res

    @staticmethod
    def _unrecognised_status_wire_value(order: Any) -> int | None:
        """The status number the broker sent, when this client has no name for it.

        An enum value this build does not know is *not* lost. Protobuf keeps it in the
        message's unknown fields, so the information behind `orderStatus` reading back as the
        first enum value - ACCEPTED - is still recoverable. Measured against the installed
        protos: a wire value of 6, 9 or 255 parses without error, reads back as 1 with
        `HasField` false, and leaves exactly one unknown field, this field's own number, with
        the original value in it. A status the broker genuinely did not send leaves no
        unknown field at all.

        That is the whole reason this function exists. Without it, "the broker sent nothing"
        and "the broker sent a status this client cannot name" are the same observation, and a
        future terminal status is read as ACCEPTED and cancelled. The two are in fact
        distinguishable, so this asks the question that can be asked and leaves the policy to
        the human. Backlog item 2.

        Returns None for anything that cannot carry the fingerprint - a test double, or a
        proto build without explicit presence - so the ordinary path is unaffected.
        """
        unknown_fields = getattr(order, "UnknownFields", None)
        fields_by_name = getattr(getattr(order, "DESCRIPTOR", None), "fields_by_name", None)
        if unknown_fields is None or fields_by_name is None:
            return None
        wanted = {
            fields_by_name[name].number
            for name in ("orderStatus", "status")
            if name in fields_by_name
        }
        if not wanted:
            return None
        for unknown in unknown_fields():
            # Wire type 0 is varint, which is how an enum arrives on the wire.
            if unknown.field_number in wanted and unknown.wire_type == 0:
                return int(unknown.data)
        return None

    def _is_working_order(self, order: Any) -> bool:
        """Whether an order is still ACCEPTED, i.e. a cancel would be legal.

        A filled or already-cancelled order must not be sent for cancellation:
        cTrader rejects it, and because that rejection propagates it would
        abort the loop and leave the *other* orders resting. Only ACCEPTED
        orders are working; anything else is already terminal.

        If the status enum is unavailable we cannot tell, and for an order we
        are about to cancel the unsafe direction is leaving it resting, so the
        caller cancels on type alone.

        A status this client has *no name for* is also a "cannot tell", but it arrives
        looking exactly like ACCEPTED. It is detected and logged - see
        :meth:`_unrecognised_status_wire_value` - while the decision stays as it was,
        because which failure is cheaper here is a trading judgement, not a test one.
        """
        unrecognised = self._unrecognised_status_wire_value(order)
        if unrecognised is not None:
            logger.warning(
                "cTrader sent order status %s, which this client has no name for. It reads as "
                "ACCEPTED, so this order is being cancelled on a status nobody here could "
                "interpret; if that status is terminal the broker will reject the cancel and "
                "abort the rest of the sweep.",
                unrecognised,
            )
        status_enum = getattr(self, "_ProtoOAOrderStatus", None)
        if status_enum is None:
            # "Cannot tell", resolved in the unsafe direction - and previously silent. The decision
            # is unchanged and stays a trading judgement; what changes is that it is now countable,
            # because a sweep here sends a cancel for *every* order it can see.
            logger.warning(
                "cTrader client exposes no order-status enum, so every order is treated as working "
                "and a cancel sweep will send a cancel for each",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            return True
        working_statuses = {int(status_enum.ORDER_STATUS_ACCEPTED)}
        # Newer protos expose `orderStatus`; older ones only `status`.
        raw_status = getattr(order, "orderStatus", None)
        if raw_status is None:
            raw_status = getattr(order, "status", None)
        if raw_status is None:
            # Also "cannot tell", also resolved by cancelling, and also silent. The
            # named-status case is already logged by `_unrecognised_status_wire_value`; this
            # is the *absent field* case, which looks identical on the wire (both read back
            # as ACCEPTED) and was logged nowhere.
            # which looks identical on the wire (both read back as ACCEPTED) and is logged nowhere.
            logger.warning(
                "cTrader order carries no status field; treating it as ACCEPTED, so it will be "
                "cancelled if a sweep runs",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "order_id": getattr(order, "orderId", None),
                    }
                },
            )
            return True
        try:
            return int(raw_status) in working_statuses
        except (TypeError, ValueError):
            return False

    async def _cancel_order_id(self, order_id: int) -> None:
        """Send one cancel for a validated positive order id."""
        cancel_req = self._ProtoOACancelOrderReq()
        cancel_req.ctidTraderAccountId = self.ctid_trader_account_id
        cancel_req.orderId = order_id
        try:
            await self._send_request_with_refresh_retry(
                cancel_req, timeout_seconds=10
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise

    async def cancel_open_orders(self) -> int:
        """Cancel every working *detached* order for this cTrader account.

        Used by the STOPPED path so a resting entry cannot fire after an
        operator asked to stop. Orders that are already filled or cancelled are
        skipped; only cancellable ones are counted.

        STOP_LOSS_TAKE_PROFIT is deliberately NOT cancellable here. That order
        type is not a resting entry: it is the stop and target *attached to an
        open position*, and cancelling it leaves that position with no stop and
        no target. A blanket cancel runs on accounts that hold positions this
        control is not about to close, and it runs before the flatten, so the
        naked-position window is not a formality: if the flatten then fails the
        position survives with every risk control removed and the deployment is
        merely logged as not-flattened. Protections that outlive their position
        are removed by :meth:`cancel_orphaned_protections` instead.
        """
        reconcile_res = await self._reconcile_state()

        # Market and market-range orders fill immediately and cannot be
        # cancelled; everything else can still be resting.
        cancellable = {
            self._ProtoOAOrderType.LIMIT,
            self._ProtoOAOrderType.STOP,
            self._ProtoOAOrderType.STOP_LIMIT,
        }
        cancelled = 0
        refused: list[int] = []
        # ProtoOAOrderListRes names the repeated field `order`, not `orders`.
        for order in getattr(reconcile_res, "order", []) or []:
            order_type = int(getattr(order, "orderType", 0) or 0)
            if order_type not in cancellable:
                continue
            if not self._is_working_order(order):
                continue
            # orderId is a protobuf int64: assigning the string form raises
            # TypeError on the wire, so coerce and skip anything unparseable.
            try:
                order_id = int(getattr(order, "orderId", 0) or 0)
            except (TypeError, ValueError):
                continue
            if order_id <= 0:
                continue
            try:
                await self._cancel_order_id(order_id)
            except CTraderApiError as exc:
                # One order's rejection must not strand the others.
                #
                # This is not a new policy - it is `_is_working_order`'s own premise. That
                # docstring justifies filtering out filled and cancelled orders precisely because
                # "cTrader rejects it, and because that rejection propagates it would abort the
                # loop and leave the *other* orders resting". The filter is a best-effort
                # inference from a status the client may not be able to name (an unrecognised
                # enum reads as ACCEPTED - item 2), so the abort the filter exists to prevent is
                # reachable anyway. Leaving orders resting is strictly worse than attempting
                # them: this function's contract is to cancel *every* working detached order,
                # and a rejection on one says nothing about the next.
                #
                # `CTraderAuthTerminalError` is a `RuntimeError`, **not** a `CTraderApiError`,
                # so this cannot swallow it: an auth-terminal failure still propagates and still
                # ends the sweep, which is correct - the connection is no longer usable and the
                # remaining cancels would fail anyway. `_raise_terminal_auth_from_api_error` is
                # called for completeness and raises if the error is auth-terminal by code or by
                # text.
                self._raise_terminal_auth_from_api_error(exc)
                refused.append(order_id)
                logger.warning(
                    "cTrader refused to cancel order %s: %s. Continuing the sweep - the remaining "
                    "orders still need cancelling.",
                    order_id,
                    exc,
                )
                continue
            cancelled += 1
        if refused:
            # Reported rather than swallowed: an operator has to be able to tell a complete sweep
            # from a partial one, and a caller seeing a lower count should be able to find out why.
            logger.warning(
                "Cancel sweep completed with %d of %d working orders refused by the broker: %s. "
                "Those orders may still be resting.",
                len(refused),
                len(refused) + cancelled,
                sorted(refused),
            )
        return cancelled

    async def cancel_orphaned_protections(self) -> int:
        """Cancel attached SL/TP orders whose position no longer exists.

        :meth:`cancel_open_orders` never disarms a live position, so closing a
        position leaves its protection behind: the order can no longer fill, it
        holds one of the account's order slots, and it makes a flattened
        account look like it still has something resting. This removes exactly
        those and nothing else:

        - the order type must be STOP_LOSS_TAKE_PROFIT (no other type is
          attached to a position);
        - its ``positionId`` must be a positive integer that is *absent* from
          the open-position set of the same snapshot. An unset or unreadable
          ``positionId`` is left alone: "I cannot tell which position this
          belongs to" must never resolve to "cancel the stop on a position that
          is still open";
        - it must be ACCEPTED, exactly like the blanket cancel.

        Anything unreadable raises, so the caller learns the account was not
        swept instead of being told a clean zero.
        """
        reconcile_res = await self._reconcile_state()
        open_position_ids: set[int] = set()
        for position in getattr(reconcile_res, "position", []) or []:
            status = int(getattr(position, "positionStatus", 0) or 0)
            if status != self._ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            try:
                position_id = int(getattr(position, "positionId", 0) or 0)
            except (TypeError, ValueError):
                continue
            if position_id > 0:
                open_position_ids.add(position_id)

        cancelled = 0
        for order in getattr(reconcile_res, "order", []) or []:
            if int(getattr(order, "orderType", 0) or 0) != (
                self._ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT
            ):
                continue
            try:
                position_id = int(getattr(order, "positionId", 0) or 0)
            except (TypeError, ValueError):
                # Unknown attachment: skip rather than guess.
                continue
            if position_id <= 0 or position_id in open_position_ids:
                continue
            if not self._is_working_order(order):
                continue
            try:
                order_id = int(getattr(order, "orderId", 0) or 0)
            except (TypeError, ValueError):
                continue
            if order_id <= 0:
                continue
            await self._cancel_order_id(order_id)
            cancelled += 1
        return cancelled

    async def close_all_positions(self) -> int:
        """Close every open position for this cTrader account."""
        positions = await self._fetch_open_positions()
        closed = 0
        for position in positions:
            # positionId and volume are protobuf int64 fields; the string form
            # cannot be assigned and an unset volume serializes as 0, which
            # cTrader rejects rather than closing the position.
            try:
                position_id = int(getattr(position, "positionId", 0) or 0)
            except (TypeError, ValueError):
                continue
            if position_id <= 0:
                continue
            trade_data = getattr(position, "tradeData", None)
            # volume is a protobuf integer field: a float raises TypeError on
            # assignment, and 0 is rejected by cTrader instead of closing.
            try:
                volume = int(_to_float(getattr(trade_data, "volume", 0) if trade_data else 0))
            except (TypeError, ValueError):
                continue
            if volume <= 0:
                continue
            close_req = self._ProtoOAClosePositionReq()
            close_req.ctidTraderAccountId = self.ctid_trader_account_id
            close_req.positionId = position_id
            close_req.volume = volume
            # volume omitted => close the full position at market.
            try:
                await self._send_request_with_refresh_retry(
                    close_req, timeout_seconds=15
                )
            except CTraderApiError as exc:
                self._raise_terminal_auth_from_api_error(exc)
                raise
            closed += 1
        return closed

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with signed qty for exposure limits and exit-only sizing.

        qty uses the same unit convention as signal metadata 'qty'
        (inverse of _resolve_order_volume: qty == broker volume / the
        symbol's volume_per_qty, #80).
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
        observed_position_ids: dict[str, list[int]] = {}
        self._position_ids_by_symbol = {}
        for position in getattr(reconcile_res, "position", []):
            status = int(getattr(position, "positionStatus", 0) or 0)
            if status != self._ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            trade_data = getattr(position, "tradeData", None)
            if trade_data is None:
                continue
            symbol_id = int(getattr(trade_data, "symbolId", 0) or 0)
            # #80: unmapped ids fail closed (no guessed FX scale).
            symbol = symbol_id_to_name.get(symbol_id)
            if symbol is None:
                raise ValueError(
                    f"cTrader symbol id {symbol_id!r} is not in the symbols "
                    "cache; refusing to scale its position with a guess."
                )
            volume = _to_float(getattr(trade_data, "volume", 0))
            qty = _broker_volume_to_qty(
                volume, self._scale_for(symbol).volume_per_qty
            )
            if int(getattr(trade_data, "tradeSide", 0) or 0) == self._ProtoOATradeSide.SELL:
                qty = -qty
            normalized_symbol = str(symbol).strip().upper()
            row: dict[str, Any] = {
                "symbol": normalized_symbol,
                "qty": qty,
                "entry_price": _to_float(getattr(position, "price", 0)),
                "current_price": 0.0,
                "unrealized_pnl": 0.0,
            }
            position_id = int(getattr(position, "positionId", 0) or 0)
            if position_id > 0:
                row["position_id"] = position_id
                observed_position_ids.setdefault(normalized_symbol, []).append(position_id)
            payload.append(row)
        self._position_ids_by_symbol = {
            symbol: tuple(position_ids)
            for symbol, position_ids in observed_position_ids.items()
        }
        return payload

    async def _persist_execution_event(self, event: Any) -> None:
        execution_type = int(event.executionType)
        if execution_type not in {
            self._ProtoOAExecutionType.ORDER_FILLED,
            self._ProtoOAExecutionType.ORDER_PARTIAL_FILL,
        }:
            return

        order = getattr(event, "order", None)
        deal = getattr(event, "deal", None)

        order_id, client_order_id = self._extract_order_and_client_ids(order, deal)
        if not client_order_id and order_id is not None:
            client_order_id = (
                await asyncio.to_thread(
                    find_live_signal_id_by_broker_order_id,
                    self.account_config_id,
                    str(order_id),
                )
                or ""
            )

        signal_id = self._parse_client_order_id(
            client_order_id, order_id=order_id, execution_type=execution_type
        )
        if signal_id is None:
            raise ValueError(
                "cTrader fill could not be mapped to a persisted live signal; "
                "retaining the event for reconciliation."
            )

        timestamp_ms = self._resolve_execution_timestamp_ms(order=order, deal=deal)
        filled_volume = self._resolve_filled_volume(order=order, deal=deal)
        if filled_volume <= 0:
            raise ValueError(
                "cTrader fill carries no positive executed volume; retaining "
                "the event for reconciliation."
            )
        # Unit contract (#80): persist filled_qty in qty units per the fill's
        # symbol scale — deal symbolId first, then the submit-time order map.
        fill_symbol: str | None = None
        if deal is not None and int(getattr(deal, "symbolId", 0) or 0):
            fill_symbol = self._symbol_name_for_id(int(getattr(deal, "symbolId", 0)))
        elif order_id is not None:
            fill_symbol = self._order_id_to_symbol.get(int(order_id))
        if fill_symbol is None:
            raise ValueError(
                "cTrader fill carries no resolvable symbol; refusing to scale "
                "its volume with a guessed symbol."
            )
        filled_qty = _broker_volume_to_qty(
            filled_volume, self._scale_for(fill_symbol).volume_per_qty
        )
        execution = LiveExecution(
            signal_id=signal_id,
            actual_price=self._resolve_execution_price(order=order, deal=deal),
            filled_qty=filled_qty,
            commission=_to_float(getattr(deal, "commission", 0.0)) if deal is not None else 0.0,
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=str(order_id or ""),
            dedupe_key=self._build_dedupe_key(
                deal, order_id=order_id, timestamp_ms=timestamp_ms,
                filled_volume=filled_volume,
            ),
            # Recorded so a later collision between two equal-sized fills is decidable from the
            # table, rather than by taking the dedupe key apart again. `None` when the broker sent
            # no deal - the case the key builder already handles by deriving a key instead.
            broker_fill_id=(
                (int(getattr(deal, "dealId", 0) or 0) or None) if deal is not None else None
            ),
        )
        persisted = await asyncio.to_thread(save_live_execution, execution)
        if not persisted:
            logger.info(
                "Skipped duplicate cTrader execution fill",
                extra={
                    "data": {
                        "signal_id": str(execution.signal_id),
                        "broker_order_id": execution.broker_order_id,
                        "execution_type": execution_type,
                    }
                },
            )
            return
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

    def _extract_order_and_client_ids(
        self, order: Any | None, deal: Any | None
    ) -> tuple[int | None, str]:
        order_id: int | None = None
        client_order_id = ""
        if order is not None:
            raw_order_id = _to_float(getattr(order, "orderId", 0), default=0.0)
            if raw_order_id > 0:
                order_id = int(raw_order_id)
            client_order_id = str(getattr(order, "clientOrderId", "") or "")
        if order_id is None and deal is not None:
            raw_order_id = _to_float(getattr(deal, "orderId", 0), default=0.0)
            if raw_order_id > 0:
                order_id = int(raw_order_id)
        if not client_order_id and order_id is not None:
            client_order_id = self._order_id_to_signal_id.get(order_id, "")
        return order_id, client_order_id

    def _parse_client_order_id(
        self,
        client_order_id: str,
        *,
        order_id: int | None,
        execution_type: int,
    ) -> UUID | None:
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
            return None
        try:
            return UUID(client_order_id)
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
            return None

    @staticmethod
    def _explicitly_sent_zero_prices(order: Any | None, deal: Any | None) -> list[str]:
        """Which sources carried a price of exactly zero that the broker *did* send.

        `executionPrice` is a proto3 `optional double`, so it has presence and an omitted field is
        distinguishable from a field set to 0.0. Measured on the installed protos:

            unset        -> HasField=False, value 0.0
            set 0.0      -> HasField=True,  value 0.0

        Only the second is interesting, because only the second looks like a price while being one.
        Returns the names of the sources that spoke so the caller can log which; empty when nothing
        was sent, or when the object cannot answer (`SimpleNamespace` doubles and other non-proto
        objects), so the existing characterisation tests are unaffected. Backlog item 10.
        """
        sent: list[str] = []
        for label, source in (("deal", deal), ("order", order)):
            if source is None:
                continue
            has_field = getattr(source, "HasField", None)
            if has_field is None:
                continue
            try:
                present = bool(has_field("executionPrice"))
            except (TypeError, ValueError):
                continue
            if present and _to_float(getattr(source, "executionPrice", 0.0)) == 0.0:
                sent.append(label)
        return sent

    @staticmethod
    def _resolve_execution_price(*, order: Any | None, deal: Any | None) -> float:
        """The best readable execution price, or 0.0 when none is readable.

        Guarded exactly as `_resolve_filled_volume` three lines below: finite and positive. It
        used to test truthiness alone, so it returned whatever the payload held - including a
        negative price and NaN. NaN was the worse of the two, because `if value:` is *true* for
        NaN: it did not even fall through to the order-level fallback, and no downstream `> 0`
        guard would catch it either. One NaN row makes `avg(actual_price)` NaN and makes
        `WHERE actual_price > x` silently skip that row.

        0.0 remains the "could not be read" answer rather than an error. What should happen to
        such a row - refuse it, flag it, or store the signal's intended price - is the open
        decision in backlog item 10, and it needs the broker's behaviour, which is not knowable
        from here.

        That decision was blocked on an ambiguity that turned out not to exist. The docstring here
        used to say: "`executionPrice` is a protobuf double *with presence* whose unset value is
        0.0, so an omitted price and a genuine zero arrive identically." Those two clauses
        contradict each other, and the measurement settles which is wrong - `optional double` does
        carry presence:

            unset        -> HasField=False, value 0.0
            set 0.0      -> HasField=True,  value 0.0
            set 261.7    -> HasField=True,  value 261.7

        So an omitted price and an explicitly-sent zero *are* distinguishable, and
        `_explicitly_sent_zero_prices` now asks which one produced the 0.0 and says so. The value
        returned is unchanged; this only makes the ambiguity visible, which is what lets the
        remaining question be answered on evidence rather than by assumption.
        """
        if deal is not None:
            value = _to_float(getattr(deal, "executionPrice", 0.0))
            if math.isfinite(value) and value > 0:
                return value
        if order is not None:
            value = _to_float(getattr(order, "executionPrice", 0.0))
            if math.isfinite(value) and value > 0:
                return value
        sent_zeros = CTraderLiveAdapter._explicitly_sent_zero_prices(order, deal)
        if sent_zeros:
            # Loud on purpose. This is the one case a reader cannot otherwise notice: the broker
            # stated a price, and stated it as zero, so nothing looks missing. Whether such a row
            # should be refused, flagged or corrected is still a trading decision - but it cannot
            # be made from a value that hides whether the broker spoke at all.
            logger.warning(
                "cTrader sent an execution price of exactly 0 on the %s, which this resolver reads "
                "as 'could not be read' and returns as 0.0. The field was present, so this is not "
                "an omitted price; if the row is stored as-is it will carry actual_price=0.",
                " and the ".join(sent_zeros),
            )
        else:
            # **And the omitted case, which was the quieter of the two.**
            #
            # The warning above covers the broker stating a price of exactly zero. This covers the
            # broker stating none at all - `executionPrice` absent on both the deal and the order.
            # Both resolve to `0.0`, both land in the row as `actual_price = 0.0`, and both make any
            # PnL derived from that row meaningless. Only one of them used to say anything.
            #
            # The sent-zero warning says "this is the one case a reader cannot otherwise notice,
            # because the broker spoke and said zero". That is true of it, and it is precisely why
            # the *other* case needs the warning too: an omitted price leaves nothing in the payload
            # at all, so the row simply carries 0.0 and a reader cannot tell a missing price from a
            # price of zero without re-deriving it from the wire.
            #
            # The stored value is unchanged, and whether such a row should be refused, flagged or
            # corrected remains a trading decision. What changes is that both causes are now
            # countable, so that decision can be made on evidence rather than on a guess.
            logger.warning(
                "cTrader sent no execution price at all (absent on both deal and order); this "
                "resolver returns 0.0 as its 'could not be read' sentinel, so a row stored as-is "
                "carries actual_price=0 with nothing in the payload to explain it"
            )
        return 0.0

    @staticmethod
    def _honour_zero_money_digits() -> bool:
        """Whether a broker-sent `moneyDigits` of 0 is honoured. Default: no.

        A copy of `account_discovery._honour_zero_money_digits`; see that one for why this is a
        flag rather than the fix. Held identical by
        `test_both_copies_of_the_money_digits_resolution_are_identical`.

        Parsed with the repo's single boolean parser (`live_runner/credential_env.py`) rather than
        a hand-rolled token set, for the reason that file gives: it was previously written out four
        times, and "four copies that agree today is the same hazard as every other duplicate this
        audit has found". A hand-rolled version here also disagreed with
        `TRUE_BOOL_TOKENS` - it dropped `y` - and silently treated a typo as *off* rather than
        raising. Failing safe is right for a money rescale, but silently is not: the whole point of
        the canonical parser is that `=treu` is a loud error.

        The absent/present split mirrors `CTRADER_DEMO`: absent means the default, present-but-blank
        is an error.

        **Known limitation, and it is a real one.** Only the process environment is consulted,
        not `.accounts.env`. Every other live flag is read file-first, and `CTRADER_DEMO` in this
        very constructor uses `resolve_env_value(env_values, ...)`, so an operator who follows the
        runbook and puts this beside `CTRADER_DEMO` gets the default, with no error and no warning.

        An earlier version of this note gave a reason that does not hold: it claimed threading an
        env mapping through would "change the signature the sync test compares". The sync test
        slices the *body* between `raw = getattr(...)` and `return int(raw)` and never looks at a
        signature, so that was simply wrong.

        The real obstacle is narrower: `_resolve_money_digits` is a staticmethod deliberately
        copied rather than imported, and has no env mapping to consult. Fixing it means threading
        one through both copies and re-pinning them, which changes the duplication guarantee
        rather than being a one-liner. Recorded as an open item in backlog item 6.
        """
        return _parse_bool_env(
            _HONOUR_ZERO_FLAG, resolve_flag_env(_HONOUR_ZERO_FLAG), default=False
        )

    @staticmethod
    def _resolve_money_digits(trader: Any, *, context: str) -> int:
        """The broker's decimal-place count, defaulting to 2, and saying so when it guesses.

        Deliberately a copy of `account_discovery._resolve_money_digits` rather than an import of
        it: this adapter runs without the Alpaca adapter loaded, and importing the discovery
        module here would pull that dependency chain into every cTrader import. The duplication is
        the lesser evil, and
        `test_both_copies_of_the_money_digits_resolution_are_identical` holds the two in sync.

        `moneyDigits` is a `uint32` **with presence**, so an unset field reads as `0`. The
        `or 2` collapse therefore cannot tell a genuine zero (this account has no minor units, and
        dividing by 100 makes the balance a hundred times too small) from the broker simply not
        sending the field (where 2 is the right conventional default). `HasField` can, so the
        ambiguous case is logged with the presence flag - which settles it on the next successful
        broker call without changing a computed value. Honouring a real zero is backlog item 6
        and needs that answer first.
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
            if present and CTraderLiveAdapter._honour_zero_money_digits():
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

    @staticmethod
    def _resolve_filled_volume(*, order: Any | None, deal: Any | None) -> float:
        if deal is not None:
            for attribute in ("filledVolume", "volume"):
                value = _to_float(getattr(deal, attribute, 0.0))
                if math.isfinite(value) and value > 0:
                    return value
        if order is not None:
            value = _to_float(getattr(order, "executedVolume", 0.0))
            if math.isfinite(value) and value > 0:
                return value
        return 0.0

    @staticmethod
    def _resolve_execution_timestamp_ms(*, order: Any | None, deal: Any | None) -> int:
        if deal is not None:
            for attribute in ("executionTimestamp", "utcLastUpdateTimestamp"):
                value = _to_float(getattr(deal, attribute, 0.0), default=0.0)
                if value > 0:
                    return int(value)
        if order is not None:
            value = _to_float(getattr(order, "utcLastUpdateTimestamp", 0.0), default=0.0)
            if value > 0:
                return int(value)
        return 0

    @staticmethod
    def _build_dedupe_key(
        deal: Any | None,
        *,
        order_id: int | None,
        timestamp_ms: int,
        filled_volume: float,
    ) -> str:
        deal_id = int(getattr(deal, "dealId", 0) or 0) if deal is not None else 0
        if deal_id > 0:
            return f"ctrader:deal:{deal_id}"
        if not timestamp_ms:
            # Loud on purpose. Without a deal id *and* without a usable timestamp the key is
            # decided entirely by order and volume, so two equal-sized fills on one order build
            # the same key and the second is dropped as a "duplicate" that was not one - the
            # position goes short and the log argues against that conclusion.
            #
            # The neighbouring fields in this path all refuse to guess: a non-positive volume is
            # rejected, an unresolvable symbol is rejected. This key was the only one that
            # degraded silently. Which policy replaces it is a trading decision, so nothing here
            # changes the key - but the degenerate case can no longer pass unremarked, so the
            # evidence for that decision arrives on its own. Backlog item 1.
            logger.warning(
                "cTrader fill has neither a deal id nor a timestamp, so its dedupe key is "
                "decided only by order %s and volume %s. Two equal-sized fills on this order "
                "would collide and the second would be dropped as a duplicate that was not one.",
                order_id or "<none>",
                filled_volume,
            )
        return f"ctrader:order:{order_id or ''}:{timestamp_ms}:{filled_volume}"

    def _requeue_execution_event(self, event: Any) -> bool:
        key = id(event)
        attempts = self._execution_retry_counts.get(key, 0)
        self._reconciliation_required = True
        self._reconciliation_retry_at = 0.0
        if attempts < EXECUTION_RETRY_LIMIT:
            self._execution_retry_counts[key] = attempts + 1
            try:
                self._execution_queue.put_nowait(event)
            except asyncio.QueueFull:
                if len(self._unresolved_execution_events) >= UNRESOLVED_EXECUTION_EVENT_LIMIT:
                    self._execution_retry_counts.pop(key, None)
                    return False
                self._unresolved_execution_events.append(event)
            return True
        if len(self._unresolved_execution_events) < UNRESOLVED_EXECUTION_EVENT_LIMIT:
            self._unresolved_execution_events.append(event)
        self._execution_retry_counts.pop(key, None)
        return False

    def _ack_execution_event(self, event: Any) -> None:
        self._execution_retry_counts.pop(id(event), None)

    async def listen_executions(self) -> None:
        backoff_seconds = 2
        while True:
            event: Any | None = None
            try:
                if not self._connected or not self._authenticated:
                    await self.connect()

                await self._run_pending_reconciliation(backoff_seconds)

                event = await asyncio.wait_for(self._execution_queue.get(), timeout=30)
                await self._persist_execution_event(event)
                self._ack_execution_event(event)
                backoff_seconds = 2
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except CTraderAuthTerminalError as exc:
                if event is not None and not self._requeue_execution_event(event):
                    raise RuntimeError(
                        "cTrader execution event could not be requeued after "
                        "terminal authentication failure; broker reconciliation "
                        "is required."
                    ) from exc
                self._log_listener_terminal_auth_failure(exc)
                return
            except Exception as exc:
                if event is not None and not self._requeue_execution_event(event):
                    raise RuntimeError(
                        "cTrader execution event exhausted its retry budget; "
                        "broker reconciliation is required before acknowledging "
                        "the fill."
                    ) from exc
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

    async def _run_pending_reconciliation(self, backoff_seconds: int) -> None:
        if not (
            self._reconciliation_required
            and time.monotonic() >= self._reconciliation_retry_at
        ):
            return
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
            unresolved = list(self._unresolved_execution_events)
            self._unresolved_execution_events.clear()
            still_unresolved: list[Any] = []
            for pending_event in unresolved:
                requeued = self._requeue_execution_event(pending_event)
                if not requeued or any(
                    id(pending_event) == id(stored_event)
                    for stored_event in self._unresolved_execution_events
                ):
                    still_unresolved.append(pending_event)
            self._unresolved_execution_events.clear()
            if still_unresolved:
                self._unresolved_execution_events.extend(
                    still_unresolved[:UNRESOLVED_EXECUTION_EVENT_LIMIT]
                )
                self._reconciliation_required = True
                self._reconciliation_retry_at = time.monotonic() + backoff_seconds
                return
            self._reconciliation_required = False
            self._reconciliation_retry_at = 0.0

    def _log_listener_terminal_auth_failure(self, exc: CTraderAuthTerminalError) -> None:
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
