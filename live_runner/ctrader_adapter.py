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
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd

from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    ORDER_STATE_FILLED,
    ORDER_STATE_TERMINAL,
    ORDER_STATE_WORKING,
    BaseLiveAdapter,
    BrokerOrderRejectedError,
    BrokerOrderView,
    ControlOutcome,
    LivePipValue,
    LiveQuote,
    ProtectiveStop,
    coerce_positive_live_qty,
    commission_from_signed_cash_flow,
    is_unbounded_tp,
    money_digits_from_trader,
    money_scale,
    order_not_submitted,
    validate_live_quote_prices,
)
from live_runner.credential_env import (
    parse_bool_env,
    read_accounts_env_file,
    resolve_accounts_env_path,
    resolve_env_value,
)
from live_runner.exit_signals import ExitBelowStepError
from risk.signal_levels import (
    SL_METADATA_KEYS,
    TP_METADATA_KEYS,
    LevelParsing,
    resolve_signal_level,
)
from shared import venue_calendar
from shared.constants import DATA_DIR
from shared.database import (
    BROKER_CLOSE_REASON_CTRADER_POSITION_CLOSE,
    broker_close_signal_id,
    find_live_signal_id_by_broker_order_id,
    find_live_signal_ids_by_broker_order_id,
    flag_live_ledger_reconciliation,
    get_live_adapter_state,
    get_live_signal_account_id,
    get_live_signal_broker_order_id,
    get_live_signal_metadata,
    get_live_signal_reconciliation_cursor,
    get_live_signal_timestamp,
    is_exit_only_signal_metadata,
    list_live_account_open_position_lots,
    list_live_ledger_reconciliation_deal_ids,
    list_live_ledger_reconciliation_flag_entries,
    list_live_signal_ids_for_reconciliation,
    list_outstanding_ledger_flag_details,
    live_execution_dedupe_key_exists,
    live_execution_dedupe_keys_present,
    record_live_broker_side_close_fill,
    resolve_ledger_reconciliation_flag,
    save_live_execution,
    save_live_execution_up_to_cumulative,
    save_live_signal,
    set_live_adapter_state,
    set_live_signal_broker_order_id,
)
from shared.logging import get_logger
from shared.lot_grid import STEP_NOISE, floor_qty_to_step
from shared.models import (
    PRICE_ABSENT,
    PRICE_OBSERVED,
    PRICE_SENT_BAD,
    LiveExecution,
    LiveSignal,
)
from shared.money import parse_money, parse_optional

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


class CTraderConnectionLostError(ConnectionError):
    """An in-flight cTrader request was abandoned by a session teardown.

    Upstream ``Client._disconnected`` clears pending response Deferreds
    without errbacking them, which would leave every in-flight
    ``_send_request`` waiting out its own response timeout after the socket
    is already gone. Teardown fails them with this instead. It is
    deliberately a plain connection error: the request may already have been
    written, so callers must treat the outcome as unknown - never as
    provably-not-submitted and never as a broker refusal.
    """


class _PendingCloseFillRetentionError(ValueError):
    """A position-close fill retained while its own close submit is unresolved.

    Subclasses ``ValueError`` so every existing retain-for-reconciliation
    handler still applies, but ``listen_executions`` recognises it and parks
    the event in ``_unresolved_execution_events`` WITHOUT spending the retry
    budget: a pending mark can live up to ``_PENDING_CLOSE_POSITION_TTL_SECONDS``
    (an unknown-outcome submit), and a budgeted requeue would crash-loop the
    listener - delaying every other fill on the account - for that whole
    window.
    """


def _format_key_quantity(value: float) -> str:
    """A stable text form for a quantity inside a dedupe key.

    `:g` switches to exponent notation at 1e6, so two different volumes could still render
    differently depending on magnitude and a key built yesterday would stop matching one built
    today. Integral volumes are rendered as integers, everything else at full precision.
    """
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return repr(numeric)


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


# ── cTrader volume unit contract ───────────────────────────────────────────────
#
# ONE unit contract for every quantity in the cTrader path:
#
#   * Signal metadata 'qty' is in 0.01-LOT units of the symbol, i.e.
#     broker_volume / volume_per_qty. The risk engine sizes in these units
#     (position_size = risk_amount / (risk_distance * pip_value)).
#     volume_per_qty = lotSize / 100 from the broker symbol properties
#     (EURUSD on the demo: 10_000_000 / 100 = 100_000), else the static
#     fallback table per symbol class (FX/JPY/XAU/XAG/index).
#   * Raw cTrader protocol fields (ProtoOANewOrderReq.volume,
#     tradeData.volume, deal.filledVolume, order.executedVolume) are in
#     BROKER VOLUME UNITS: broker_volume == qty * volume_per_qty. Broker
#     volume (and lotSize) is in CENTS of the base asset, so one qty unit is
#     volume_per_qty / 100 base units (EURUSD: 1,000 EUR).
#   * pip_value (#146) is the account-currency P&L of one qty unit per 1.0
#     price move: volume_per_qty / 100 * (quote -> account rate), from
#     CTraderLiveAdapter.get_pip_value. Entries are only sized from a
#     broker-sent lotSize: the static fallback table records lot sizes in
#     base units, 100x below the broker's cents for FX/XAU (#184), and is
#     never used for sizing.
#
# Every crossing of that boundary goes through _qty_to_broker_volume /
# _broker_volume_to_qty with the symbol's scale (adapter._scale_for, #80:
# broker props first, fallback table second, unknown symbols raise).


#: Legacy FX scale, kept as the pinned default for the module-level helpers
#: and the FX fallback entry below (qty 5.0 -> volume 5000).
QTY_VOLUME_SCALE = 1000.0
#: cTrader volumes (order volume, ``lotSize``, ``minVolume``, ``stepVolume``)
#: are in CENTS of the base asset: volume 100_000 = 1,000 units (EURUSD 0.01
#: lot). Verified on the demo: EURUSD lotSize 10_000_000 (100,000 EUR),
#: XAUUSD 10_000 (100 oz), US500 100 (1 index unit).
CTRADER_VOLUME_PER_BASE_UNIT = 100.0
#: Upper bound on the broker's quote->account conversion chain (#146). The
#: demo returns one hop (JPY->USD via USDJPY); a longer chain is unexpected.
MAX_CONVERSION_HOPS = 3
CTRADER_RELATIVE_PRICE_UNIT_DIVISOR = 100_000
EXECUTION_RETRY_LIMIT = 3
UNRESOLVED_EXECUTION_EVENT_LIMIT = 1000
#: How long a ``ClosePositionReq`` submit keeps its position marked pending
#: (position_id -> (signal_id, monotonic timestamp)). ``ProtoOAClosePositionReq``
#: carries no ``clientOrderId`` of ours, so while the submit is in flight - or
#: after an unknown-outcome submit until reconciliation persists the broker
#: order id - a fill on the position can only be retained, not attributed.
#: The mark expires so a failed submit cannot hold a genuine broker-side
#: close on the position out of the stream path indefinitely. The TTL is a
#: bound, not a guarantee: a submit whose broker outcome stays unknown past
#: it loses retention, and a later fill on the position books as a
#: broker-side close - acceptable because an unknown-outcome submit is
#: already an operator-visible reconciliation case. The TTL must stay well
#: above the reconciliation cadence so an ordinary in-flight submit cannot
#: outlive it.
_PENDING_CLOSE_POSITION_TTL_SECONDS = 3600.0
#: How often a parked pending-close fill triggers a reconciliation pass while
#: its submit's mark is still live. Budget-free parking would otherwise never
#: re-arm the pass that can recover the close's broker order id.
_PENDING_CLOSE_FILL_RECONCILE_SECONDS = 30.0
#: Bounded broker-state confirmation for ProtoOAAmendPositionSLTPReq (#168):
#: the request has no dedicated response, so the amended stop is confirmed by
#: re-reading the position. The broker applies the amend asynchronously, so a
#: few short-interval reads cover the apply window without stalling the trail.
_AMEND_CONFIRM_ATTEMPTS = 8
_AMEND_CONFIRM_INTERVAL_SECONDS = 0.4
#: Sentinel queued on ``_execution_queue`` to wake ``listen_executions`` for a
#: pending broker reconciliation instead of letting it sleep out the 30 s
#: ``queue.get`` timeout. Never a real execution payload.
_EXECUTION_RECONCILE_WAKE = object()

#: How long after our own token rotation (refresh or adoption of a sibling's
#: persisted pair) a ``ProtoOAAccountsTokenInvalidatedEvent`` is treated as
#: the echo of THAT rotation rather than a fresh invalidation of the current
#: token. cTrader emits the event when a token is refreshed, so the echo can
#: arrive after our re-auth and even after the resent request; tearing the
#: session down then would turn a recoverable submit into an unknown outcome.
#: A genuinely invalidated current token still surfaces through the next
#: request's auth error, which drives the refresh path - so a bounded window
#: errs toward keeping a healthy session. The window must stay larger than
#: cTrader's invalidation-event delivery latency or a late echo for the OLD
#: token would tear down the re-authenticated session. The grace applies ONLY
#: when the session already re-authenticated on the current token
#: (``_session_access_token == access_token``): while a rotation+re-auth is
#: still in flight the explicit ``_own_token_rotation_in_flight`` flag is the
#: skip signal instead, and a stale session token means the session is dead.
_TOKEN_INVALIDATION_OWN_ROTATION_GRACE_SECONDS = 60.0

#: Minimum interval between ``listen_executions`` reconnect attempts after a
#: session teardown. The teardown wakes the listener immediately (it used to
#: wait out the 30 s queue timeout); without a floor, a broker that keeps
#: dropping the session right after auth would produce an unbounded
#: connect/auth/disconnect loop bounded only by connect latency.
_LISTENER_RECONNECT_MIN_INTERVAL_SECONDS = 2.0

#: One refresh lock per (event loop, accounts env file). Every cTrader
#: adapter in the process shares the same persisted token pair, and a cTrader
#: refresh token is single-use: without serialization two adapters could
#: spend the same refresh token concurrently and the loser would latch
#: terminal. Keyed by loop because ``asyncio.Lock`` binds to the loop that
#: first contends it; the daemon runs a single loop, so this is exact there.
#: Adapters refreshing from more than one event loop in the same process are
#: NOT serialized by this dict - that configuration is unsupported.
_SHARED_TOKEN_REFRESH_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}


def _shared_token_refresh_lock(accounts_env_path: Path) -> asyncio.Lock:
    # Canonicalize so adapters constructed with differently-formed paths to
    # the same env file (relative vs absolute, symlink variants) share one
    # lock - otherwise their refreshes are not serialized.
    key = (
        asyncio.get_running_loop(),
        str(accounts_env_path.expanduser().resolve()),
    )
    lock = _SHARED_TOKEN_REFRESH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SHARED_TOKEN_REFRESH_LOCKS[key] = lock
    return lock


# ── Trendbar market data (#168) ────────────────────────────────────────────────
#
# ProtoOATrendbar is delta-encoded against its own LOW, not the previous close:
#   low    = absolute price in 1/100000 units (int64)
#   open   = (low + deltaOpen) / 100000
#   close  = (low + deltaClose) / 100000
#   high   = (low + deltaHigh) / 100000
#   utcTimestampInMinutes = unix minutes of the bar's nominal open
# (cTrader Open API model-messages docs; verified against the installed
# ctrader_open_api 0.9.2 descriptors.)

#: Deployment/signal timeframe spelling -> ProtoOATrendbarPeriod member name.
#: Only periods the enum actually defines are mapped (there is no H2/H3/H6).
_TRENDBAR_PERIOD_BY_TIMEFRAME: dict[str, str] = {
    "1m": "M1",
    "1min": "M1",
    "2m": "M2",
    "3m": "M3",
    "4m": "M4",
    "5m": "M5",
    "5min": "M5",
    "10m": "M10",
    "15m": "M15",
    "15min": "M15",
    "30m": "M30",
    "30min": "M30",
    "1h": "H1",
    "60min": "H1",
    "4h": "H4",
    "12h": "H12",
    "1d": "D1",
    "1w": "W1",
}

#: Seconds per ProtoOATrendbarPeriod member (the enum has no H2/H3/H6/D2+).
_TRENDBAR_PERIOD_SECONDS: dict[str, int] = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "H12": 43200,
    "D1": 86400,
    "W1": 604800,
    "MN1": 2_592_000,
}

#: Canonical deployment timeframes the cTrader adapter can serve for the
#: live ``supertrend_flip`` workflow (#168). Only sub-day bars: the broker
#: anchors ``4h``/``1d``/``1w`` to its server day/week, which is not provably
#: the UTC alignment replay uses, so the flip workflow refuses them. The
#: per-broker live-trailing table in ``shared.trailing`` mirrors this set.
CTRADER_MARKET_DATA_TIMEFRAMES: frozenset[str] = frozenset(
    {"1m", "5m", "15m", "30m", "1h"}
)


def _trendbar_period_for_timeframe(timeframe: str) -> str | None:
    """The ProtoOATrendbarPeriod member name for a deployment timeframe."""
    normalized = str(timeframe or "").strip().lower()
    return _TRENDBAR_PERIOD_BY_TIMEFRAME.get(normalized)


def _decode_trendbar_price(raw_value: Any, *, price_digits: int) -> float:
    """One 1/100000-unit price field -> a symbol-precision float, fail closed."""
    try:
        raw = float(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"cTrader trendbar price is not numeric: {raw_value!r}") from exc
    if not math.isfinite(raw):
        raise ValueError(f"cTrader trendbar price is not finite: {raw_value!r}")
    price = raw / CTRADER_RELATIVE_PRICE_UNIT_DIVISOR
    # Round to the symbol's own digits, exactly like the broker-doc recipe:
    # the integer protocol value can carry sub-tick noise the symbol cannot.
    return round(price, max(0, int(price_digits)))


def _trendbar_present(bar: Any, has_field: Any, name: str) -> bool:
    if has_field is None:
        return getattr(bar, name, None) is not None
    try:
        return bool(has_field(name))
    except (TypeError, ValueError):
        return getattr(bar, name, None) is not None


def _trendbar_field(bar: Any, name: str) -> float:
    try:
        return float(getattr(bar, name))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"cTrader trendbar field {name!r} is unreadable") from exc


def _require_trendbar_fields(bar: Any, has_field: Any) -> None:
    for name in ("low", "deltaOpen", "deltaClose", "deltaHigh"):
        if not _trendbar_present(bar, has_field, name):
            raise ValueError(f"cTrader trendbar is missing required field {name!r}")


def _validate_trendbar_prices(prices: Mapping[str, float]) -> None:
    if min(prices.values()) <= 0:
        raise ValueError(
            "cTrader trendbar decoded a non-positive price: "
            f"O={prices['open']!r} H={prices['high']!r} "
            f"L={prices['low']!r} C={prices['close']!r}"
        )
    if prices["high"] < max(prices["open"], prices["close"], prices["low"]) or (
        prices["low"] > min(prices["open"], prices["close"])
    ):
        raise ValueError(
            "cTrader trendbar OHLC is inconsistent: "
            f"O={prices['open']!r} H={prices['high']!r} "
            f"L={prices['low']!r} C={prices['close']!r}"
        )


def _trendbar_open_time(bar: Any, timestamp_ms_fallback: int | None) -> datetime:
    open_minutes = _optional_int(getattr(bar, "utcTimestampInMinutes", None))
    if open_minutes is not None and open_minutes > 0:
        return datetime.fromtimestamp(open_minutes * 60, tz=timezone.utc)
    if timestamp_ms_fallback is not None and timestamp_ms_fallback > 0:
        return datetime.fromtimestamp(timestamp_ms_fallback / 1000.0, tz=timezone.utc)
    raise ValueError("cTrader trendbar has no usable open timestamp")


def decode_trendbar(
    bar: Any,
    *,
    price_digits: int,
    timestamp_ms_fallback: int | None = None,
) -> dict[str, Any]:
    """Decode one ``ProtoOATrendbar`` into an OHLCV row.

    ``timestamp_ms_fallback`` is the deprecated parallel ``timestamp`` array
    entry (unix ms), used only when the bar carries no
    ``utcTimestampInMinutes``. Raises ``ValueError`` on any missing or
    unusable field: a bar this decoder cannot read must not be guessed into
    the series a workflow trades on.
    """
    has_field = getattr(bar, "HasField", None)
    _require_trendbar_fields(bar, has_field)
    low_raw = _trendbar_field(bar, "low")
    prices = {
        "open": _decode_trendbar_price(
            low_raw + _trendbar_field(bar, "deltaOpen"), price_digits=price_digits
        ),
        "high": _decode_trendbar_price(
            low_raw + _trendbar_field(bar, "deltaHigh"), price_digits=price_digits
        ),
        "low": _decode_trendbar_price(low_raw, price_digits=price_digits),
        "close": _decode_trendbar_price(
            low_raw + _trendbar_field(bar, "deltaClose"), price_digits=price_digits
        ),
    }
    _validate_trendbar_prices(prices)
    return {
        "timestamp": _trendbar_open_time(bar, timestamp_ms_fallback),
        "open": prices["open"],
        "high": prices["high"],
        "low": prices["low"],
        "close": prices["close"],
        "volume": float(getattr(bar, "volume", 0) or 0),
    }


def decode_trendbars_response(
    response: Any, *, symbol: str, price_digits: int
) -> list[dict[str, Any]]:
    """Decode every bar of one ``ProtoOAGetTrendbarsRes``, oldest first.

    Bars carry their own ``utcTimestampInMinutes``; a per-bar fallback array is
    only honoured when the response's deprecated ``timestamp`` field is
    genuinely iterable (it is a singular int64 in the shipped proto).
    """
    bars = list(getattr(response, "trendbar", []) or [])
    # ``ProtoOAGetTrendbarsRes.timestamp`` is a singular deprecated int64 (an
    # echo of the request's ``toTimestamp`` - cTrader's docs say "simply don't
    # use this field"), NOT the parallel per-bar unix-ms array an older proto
    # shape had. Iterating the scalar raised ``TypeError: 'int' object is not
    # iterable`` on every fetch the broker populated it on, which is what
    # failed every cTrader internal-runner symbol in the 2026-11-24 soak. Only
    # a genuinely iterable value can serve as a per-bar fallback; a scalar is
    # ignored and bars keep their own ``utcTimestampInMinutes``.
    raw_timestamps = getattr(response, "timestamp", None)
    if isinstance(raw_timestamps, int):
        raw_timestamps = None
    timestamps = [int(value) for value in (raw_timestamps or [])]
    rows: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        fallback = timestamps[index] if index < len(timestamps) else None
        try:
            rows.append(
                decode_trendbar(
                    bar,
                    price_digits=price_digits,
                    timestamp_ms_fallback=fallback,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"cTrader trendbar {index} for {symbol} is unusable: {exc}"
            ) from exc
    rows.sort(key=lambda row: row["timestamp"])
    return rows


#: Per-request span of the broker-close deal-list pass (#144): the pass pages
#: backwards in windows of this size from ``now`` down to its floor (see
#: ``_reconcile_broker_side_closes``). A window that still reports ``hasMore``
#: is re-fetched halved, down to ``BROKER_CLOSE_RECONCILE_MIN_WINDOW_MS``
#: before the truncation is only logged.
BROKER_CLOSE_RECONCILE_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
BROKER_CLOSE_RECONCILE_MAX_ROWS = 1000
#: Smallest deal-list window the pass will subdivide to before accepting the
#: broker's ``hasMore`` truncation as a loud warning instead of paging further.
BROKER_CLOSE_RECONCILE_MIN_WINDOW_MS = 60 * 60 * 1000
#: How far back the paged pass may reach at all. A close older than this - or
#: one the deal list cannot attribute - stays with the ledger-mismatch halt
#: and operator reconciliation; the pass bounds its connect-time cost rather
#: than brute-forcing years of account deal history.
BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS = 400 * 24 * 60 * 60 * 1000
#: Slack subtracted from the oldest open exposure's timestamp: an opening
#: deal cannot precede its signal by more than broker/local clock skew and
#: order lead time, but the margin keeps a skewed clock from hiding a close.
BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS = 24 * 60 * 60 * 1000
#: Hard cap on ``ProtoOADealListReq`` calls in one reconcile pass. cTrader
#: allows 5 historical requests/second/connection; at the pacing interval
#: below this budget is ~18s of broker reads worst case, and the pass scans
#: newest-first so a cap can only drop the oldest windows (which are logged).
BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS = 72
#: Pace deal-list reads below the documented 5/s historical-data limit.
BROKER_CLOSE_RECONCILE_DEAL_LIST_MIN_INTERVAL_SECONDS = 0.25
#: Deepest ``hasMore`` subdivision the pass will try before recording the
#: unpaged range; the request budget above is the real bound.
BROKER_CLOSE_RECONCILE_MAX_SUBDIVISION_DEPTH = 4
#: ``live_adapter_state`` key holding the per-account broker-close scan
#: cursor: which deal-time ranges are still uncovered, so a request-capped
#: pass resumes where it stopped instead of restarting at ``now`` every
#: pass and starving the oldest windows forever.
BROKER_CLOSE_SCAN_STATE_KEY = "ctrader_broker_close_scan"
#: Slack around a flagged deal's timestamp when its window is re-queued for
#: a booking retry: deal-list ``from/to`` bounds are half-open, and clock
#: skew between the event timestamp and the deal row must not drop it.
BROKER_CLOSE_FLAGGED_RETRY_MARGIN_MS = 5 * 60 * 1000
#: Flag causes worth retrying on later passes. These are the transient
#: ones: the order list was unreadable, the record was refused, or the
#: opener was not found inside the lookup window. Deterministic causes
#: (``no_position_id``, ``ambiguous_parent``, ``unusable_*``) cannot
#: resolve by retrying, so their windows are not re-scanned.
BROKER_CLOSE_FLAGGED_RETRYABLE_CAUSES = frozenset(
    {"position_orders_unreadable", "record_refused", "no_opening_order"}
)
#: Consecutive deal-window failures that end the pass's scan early: a
#: flapping broker must not turn one reconcile into a timeout per pending
#: range inside the connect lock.
BROKER_CLOSE_SCAN_MAX_CONSECUTIVE_FAILURES = 3
#: How far below the last covered top the fresh scan gap starts. A deal the
#: deal list had not yet shown at the previous pass's ``now`` - broker-side
#: visibility lag, or local clock ahead of broker time - lands below the old
#: top and would be skipped forever; deal-id dedupe makes re-reading it free.
BROKER_CLOSE_SCAN_CURSOR_OVERLAP_MS = 60 * 60 * 1000
#: Flag causes a booking can never clear: they name no bookable deal or the
#: attribution itself was refused, so they keep the ``account:symbol``
#: target unresolved until an operator resolves it.
_LEDGER_FLAG_UNRESOLVABLE_CAUSES = frozenset(
    {"ambiguous_parent", "no_position_id"}
)


def _power_of_ten_exponent(value: int) -> int:
    """Return ``n`` for ``value == 10 ** n``; fail loudly for any other value."""
    exponent = len(str(value)) - 1 if value > 0 else -1
    if exponent < 0 or 10**exponent != value:
        raise ValueError(f"Expected a power of ten, got {value!r}.")
    return exponent


def _relative_distance(
    symbol: str,
    reference_price: float,
    price: float,
    scale: int,
    grid: int,
    *,
    is_stop: bool,
) -> int:
    """Positive SL/TP distance in 1/``scale`` price units, floored to ``grid``.

    Floors toward the entry so a stop never widens beyond the risk the position was
    sized for. A stop under one tick is refused; a take-profit under one tick is
    sent as one tick.
    """
    leg = "SL" if is_stop else "TP"
    if math.isnan(price) or math.isinf(price) or price <= 0:
        raise ValueError(f"Invalid {leg} price for {symbol}: {price!r}; refusing to submit.")
    distance = abs(reference_price - price)
    if distance <= 0:
        raise ValueError(
            f"{leg} price {price!r} equals the reference price {reference_price!r} "
            f"for {symbol}; refusing to submit a zero-distance {leg}."
        )
    raw = distance * scale
    if not math.isfinite(raw) or raw < 1:
        raise ValueError(
            f"{leg} distance for {symbol} resolves to {raw!r} "
            f"(below one 1/{scale} price unit); refusing to submit."
        )
    if is_stop and raw < grid * (1 - 1e-9):  # float noise at exactly one tick
        raise ValueError(
            f"{leg} distance for {symbol} is below one tick of the symbol "
            f"price grid ({raw!r} < {grid} protocol units); refusing to submit "
            "rather than widen the stop beyond the sized risk."
        )
    return max(grid, math.floor(raw / grid + 1e-9) * grid)


def _utc_now() -> datetime:
    """Receive-time clock for spot events (a seam so tests can freeze it)."""
    return datetime.now(timezone.utc)


@dataclasses.dataclass
class _SpotWaiter:
    """Collects one bid/ask from spot events for a pending :meth:`get_quote`.

    cTrader spot events may carry only the side that changed, so the two
    sides are accumulated across events received after the subscription.
    ``timestamp`` is the latest broker spot time seen; ``received_at`` the
    local time the completing event arrived, used only when the broker sent
    no time at all.
    """

    future: asyncio.Future[LiveQuote]
    bid: float | None = None
    ask: float | None = None
    timestamp: datetime | None = None
    received_at: datetime | None = None


# Sentinel returned by ``_present_proto_field`` when field presence cannot be
# proven. For refusal classification "absent" would release the order lease,
# so an unanswerable ``HasField`` must read as "present / unknown" instead.
_PRESENT_BUT_UNKNOWN = object()


def _present_proto_field(
    payload: Any, name: str, *, fail_closed: bool = False
) -> Any | None:
    """The field's value when it was sent, else ``None``.

    Spot ``bid``/``ask`` are ``optional uint64``: ``HasField`` tells an omitted
    side from a sent one. A payload without ``HasField`` (a test double) falls
    back to attribute presence. With ``fail_closed`` a ``HasField`` failure
    reports the field as present but unknown (the sentinel) rather than
    absent, for callers where "absent" is the unsafe direction.
    """
    has_field = getattr(payload, "HasField", None)
    if has_field is not None:
        try:
            if not bool(has_field(name)):
                return None
        except Exception:  # except-kind: money-decision
            # Presence is unanswerable: fail_closed callers get the sentinel,
            # everyone else reads the field as absent - never as proof. The
            # catch is deliberately wider than (TypeError, ValueError):
            # HasField is protobuf-version-dependent and test doubles raise
            # arbitrary errors, and EVERY exception here means the same thing -
            # presence could not be proven - so the fail-closed policy applies
            # uniformly. Logged because a swallowed HasField failure is
            # otherwise invisible.
            logger.warning(
                "cTrader HasField(%s) failed; treating field presence as "
                "unanswerable",
                name,
                exc_info=True,
            )
            if fail_closed:
                return _PRESENT_BUT_UNKNOWN
            return None
    return getattr(payload, name, None)


def _present_proto_field_fail_closed(payload: Any, name: str) -> Any | None:
    """``_present_proto_field`` for refusal classification.

    Every field check on the order-refusal path MUST go through this wrapper:
    an unanswerable ``HasField`` reads as present-but-unknown, never absent,
    because "absent" is the direction that would release the submission lease.
    """
    return _present_proto_field(payload, name, fail_closed=True)


@dataclasses.dataclass(frozen=True)
class SymbolScale:
    """Per-symbol broker scaling (#80).

    ``volume_per_qty``: broker volume units per risk-engine qty unit.
    The qty unit is 0.01 lots, so ``volume_per_qty = lotSize / 100``
    (broker EURUSD: 10_000_000/100 = 100_000; the fallback table's FX
    100_000/100 = 1000 is in base units, see the unit contract above).
    ``price_digits``: broker-reported symbol price decimals. Relative SL/TP units
    always use the fixed protocol divisor; ``price_digits`` only sizes the price-grid
    the distance is floored to, toward the entry (``10 ** (5 - price_digits)`` units,
    1 if finer).
    ``min_volume``: broker minimum order volume (fail-closed floor).
    ``source``: "broker" (ProtoOASymbol props) or "fallback" (static table).
    ``lot_size_from_broker``: the broker sent ``lotSize`` itself. A "broker"
    scale may still borrow a missing ``lotSize`` from the fallback table; the
    pip value (#146) is only derived from a broker-sent one.
    ``step_volume``: broker volume increment (``stepVolume``). cTrader rejects
    any order volume that is not a multiple of it (``TRADING_BAD_VOLUME``), so
    entries and exits are both floored onto this grid. Unknown means one broker
    volume unit (no known grid), so a full close of the real position volume is
    never cut short.
    """

    volume_per_qty: float
    price_digits: int
    min_volume: int
    lot_size: float
    source: str = "fallback"
    lot_size_from_broker: bool = False
    step_volume: int = 1


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


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fallback_field(fallback: SymbolScale | None, name: str, default: Any = None) -> Any:
    return getattr(fallback, name) if fallback is not None else default


def _symbol_scale_from_broker_props(
    *,
    digits: int | None,
    lot_size: float | None,
    min_volume: int | None,
    fallback: SymbolScale | None,
    step_volume: Any = None,
) -> SymbolScale | None:
    """Build a broker-authoritative scale, using a fallback only when available."""
    digit_count = _optional_int(digits)
    if digit_count is None or not 0 <= digit_count <= 10:
        digit_count = _fallback_field(fallback, "price_digits")
    lots = _optional_float(lot_size)
    lots_from_broker = lots is not None and math.isfinite(lots) and lots > 0
    if not lots_from_broker:
        lots = _fallback_field(fallback, "lot_size")
    minimum = _optional_int(min_volume)
    if minimum is None or minimum <= 0:
        minimum = _fallback_field(fallback, "min_volume")
    if digit_count is None or lots is None or minimum is None:
        return None
    step = _optional_int(step_volume)
    if step is None or step <= 0:
        # Protobuf reports an unset optional as 0. One unit never cuts a full
        # close short; a wrong partial is the broker's to reject, never upsized.
        step = _fallback_field(fallback, "step_volume", 1)
    return SymbolScale(
        volume_per_qty=lots / 100.0,
        price_digits=digit_count,
        min_volume=minimum,
        lot_size=lots,
        source="broker",
        lot_size_from_broker=lots_from_broker,
        step_volume=step,
    )


def _qty_to_broker_volume(qty: float, volume_per_qty: float = QTY_VOLUME_SCALE) -> int:
    """Convert qty units (risk-engine units) into broker volume units."""
    qty_value = coerce_positive_live_qty(qty)
    if not math.isfinite(float(volume_per_qty)) or float(volume_per_qty) <= 0:
        raise ValueError("volume_per_qty must be finite and positive")
    # Floor, never round: rounding to nearest can size up by half a volume unit
    # and put the loss at the stop above the risk budget. The shared lot-grid
    # floor's decimal noise band absorbs binary float error
    # (0.29 * 100 == 28.999999999999996) and can return a value up to 1e-6 of
    # a unit above a real fraction that close to the next unit -- below every
    # broker's volume resolution -- #278.
    volume = int(floor_qty_to_step(qty_value * float(volume_per_qty), 1.0))
    if volume <= 0:
        raise ValueError(
            f"qty {qty_value!r} is below one broker volume unit at "
            f"{float(volume_per_qty)!r} units per qty; refusing to round it up "
            "past the risk budget."
        )
    return int(volume)


def _validate_broker_volume(volume: Any, *, field_name: str) -> int:
    """Validate the broker volume a request builder was handed, failing closed.

    Both builders receive the volume ``submit_order`` resolved once, as a
    Python ``int`` count of whole broker volume units - the protocol fields are
    int32, and a whole-valued ``float`` is refused rather than accepted: this
    seam is a typed contract, not a coercion point. A fractional, boolean or
    textual value means the caller handed this seam something else - qty, or a
    scaled quantity - and rounding it would put a size on the wire that nobody
    decided on. The two builders must agree on this contract, so the check
    lives here rather than in each of them.
    """
    if isinstance(volume, bool) or not isinstance(volume, int):
        raise ValueError(
            f"{field_name} must be an int count of broker volume units, got "
            f"{volume!r}; refusing to round or coerce a size."
        )
    broker_volume = int(volume)
    if broker_volume <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return broker_volume


def _floor_to_volume_step(volume: int, step_volume: int) -> int:
    """Floor *volume* onto the broker's volume grid.

    cTrader rejects an order whose volume is not a multiple of the symbol's
    ``stepVolume`` (``TRADING_BAD_VOLUME``). A risk-sized volume almost never
    lands on that grid, so it is rounded DOWN: rounding up would exceed the
    risk budget. A result below the broker minimum is refused by the caller.
    Both operands are integers, so ``//`` is already the exact floor -- there
    is no float dust for the shared lot-grid floor (#278) to absorb, and
    routing ints through it could only add error.
    """
    step = int(step_volume)
    if step <= 1:
        return int(volume)
    return (int(volume) // step) * step


def _broker_volume_to_qty(volume: float, volume_per_qty: float = QTY_VOLUME_SCALE) -> float:
    """Convert broker volume units into qty units (risk-engine units)."""
    return parse_money(volume) / float(volume_per_qty)


def _enum_name_or_none(name_fn: Any, value: int) -> str | None:
    try:
        return str(name_fn(value))
    except Exception:  # except-kind: loop-survival
        return None


def _light_symbol_assets(symbol: Any) -> tuple[int, int] | None:
    """(baseAssetId, quoteAssetId) of a ``ProtoOALightSymbol``, or ``None``.

    Both are optional on the light symbol; an unset id reads as 0, which is
    never a real asset, so it is treated as missing.
    """
    try:
        base = int(getattr(symbol, "baseAssetId", 0) or 0)
        quote = int(getattr(symbol, "quoteAssetId", 0) or 0)
    except (TypeError, ValueError):
        return None
    if base <= 0 or quote <= 0:
        return None
    return base, quote


def _conversion_quote_prices(quote: Any, *, symbol: str) -> tuple[float, float]:
    """Validated (bid, ask) of a conversion-rate quote, failing closed (#146).

    The rules are shared with the daemon's entry gate
    (:func:`live_runner.base_adapter.validate_live_quote_prices`); only the label is ours.
    """
    return validate_live_quote_prices(quote, symbol=symbol, label="cTrader conversion quote")


def _deal_id_or_none(deal: Any | None) -> int | None:
    """The broker's per-fill id, or ``None`` when it did not send one.

    ``0`` is both the protobuf default for an unset ``dealId`` and the sentinel this adapter
    rejects, so it must never be stored as a real id: a fabricated ``0`` would make "the broker
    told us nothing" indistinguishable from "the broker said zero".
    """
    if deal is None:
        return None
    deal_id = int(getattr(deal, "dealId", 0) or 0)
    return deal_id if deal_id > 0 else None


def _lot_timestamp_ms(lot: Mapping[str, Any]) -> int | None:
    """An open-lot timestamp as unix ms, tolerant of DB datetime or epoch values."""
    timestamp = lot.get("timestamp")
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return int(timestamp.timestamp() * 1000)
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return int(timestamp)
    return None


def _deal_commission(
    deal: Any | None, *, rebate_logged: set[int] | None = None
) -> float | None:
    """A deal's commission in account currency, as a positive cost — or ``None``.

    cTrader reports a charge as a NEGATIVE ``commission`` (a signed cash flow). It is stored as a
    positive cost, the ``live_executions.commission`` convention shared with Alpaca (see
    ``base_adapter.commission_from_signed_cash_flow``).

    ``ProtoOADeal.commission`` is an ``int64`` in minor units: it is scaled by the deal's own
    ``moneyDigits`` exactly like ``ProtoOATrader.balance`` is by the trader's. Storing the raw
    integer records a 2-digit-account commission of 3.50 as 350. A deal that carries no
    commission (an unset int64 reads as ``0``) is ``0.0`` without consulting ``moneyDigits``, so
    only a real charge can trigger the "digits missing" fallback warning.

    A positive broker cash flow is a REBATE: the converted cost is negative, which the
    ``commission`` write guard refuses. Refusing the ``LiveExecution`` would drop the whole
    fill — the stream caller retains the event for reconciliation forever and the row never
    reaches ``live_executions``, the lost-fill the persist path exists to prevent — so the
    rebate is stored as ``None`` ("fee unknown"; readers already treat it as net-PnL
    incomplete) and logged at ERROR with the deal id and the raw value. How a rebate should
    be represented is an open maintainer decision (#285); until then the fill is kept.
    """
    if deal is None:
        return 0.0
    raw = parse_money(getattr(deal, "commission", 0))
    # Truthiness, not float equality: an unset int64 (and a test double's 0.0) is falsy.
    if not raw:
        return 0.0
    cost = commission_from_signed_cash_flow(raw / money_scale(money_digits_from_trader(deal)))
    if cost < 0:
        deal_id = _deal_id_or_none(deal)
        # ERROR once per deal id per adapter: reconciliation re-reads the same deals on
        # every pass, and each ERROR is a Sentry event — the anomaly is logged loudly
        # once, not once per pass.
        if rebate_logged is None or deal_id is None or deal_id not in rebate_logged:
            logger.error(
                "cTrader deal %s reported a rebate: commission=%r converts to cost=%r, which "
                "the write guard refuses; storing commission=NULL so the fill is kept and "
                "net PnL stays flagged incomplete (#285)",
                deal_id,
                raw,
                cost,
            )
            if rebate_logged is not None and deal_id is not None:
                rebate_logged.add(deal_id)
        return None
    return cost


_NOT_AUTHORIZED_MARKER = "not authorized"

#: `str()` of the exception cTrader's client raises on a response timeout:
#: ``(15, 'Deferred')`` - see `_classify_deferred_response_timeout`.
_RESPONSE_TIMEOUT_SHAPE = re.compile(r"^\(\s*(\d+)\s*,\s*['\"]Deferred['\"]\s*\)$")


def _twisted_deferred_timeout_class() -> type[BaseException] | None:
    """`twisted.internet.defer.TimeoutError`, or None if Twisted cannot be imported.

    Imported lazily and by name: this module is loaded by the test suite and by deployments
    without a cTrader install, and a module-scope Twisted import would be a side effect this
    file otherwise does not have.
    """
    try:
        from twisted.internet import defer
    except Exception:  # pragma: no cover - Twisted is absent  # except-kind: loop-survival
        return None
    return getattr(defer, "TimeoutError", None)


def _classify_deferred_response_timeout(
    exc: BaseException, error_text: str
) -> tuple[bool, int | None]:
    """Is `exc` cTrader's *response* timeout, and how long did it wait?

    `ctrader_open_api/client.py` calls `responseDeferred.addTimeout(...)`, and Twisted answers
    expiry with `raise TimeoutError(timeout, "Deferred")`. So `args == (15, "Deferred")`,
    `str(exc)` is `(15, 'Deferred')`, and 15 is the configured response timeout. The request
    reached a socket that accepted it and nothing came back - so it is neither a credentials
    problem nor a TLS failure, and reporting it as "auth" sends an operator to refresh a token
    that works.

    Two signals, because either alone is incomplete. The class is authoritative when Twisted is
    importable; the string shape covers the case where it is not, and is deliberately narrow -
    anchored at both ends and requiring the literal `Deferred`, so a merely-similar message falls
    through to the other branches. Twisted's `TimeoutError` is its own class deriving straight
    from `Exception`, not the builtin, which on 3.11+ is also `asyncio.TimeoutError` - so an
    `isinstance(exc, asyncio.TimeoutError)` test cannot recognise it.
    """
    seconds: int | None = None
    match = _RESPONSE_TIMEOUT_SHAPE.match(str(error_text).strip())
    if match:
        seconds = int(match.group(1))

    timeout_class = _twisted_deferred_timeout_class()
    if timeout_class is None or not isinstance(exc, timeout_class):
        return bool(match), seconds

    if seconds is None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int) and not isinstance(args[0], bool):
            seconds = int(args[0])
    return True, seconds


def _payload_ctid_trader_account_id(payload: Any) -> int:
    """``ctidTraderAccountId`` on an inbound payload, ``0`` when unset or unreadable.

    ``ProtoOAErrorRes.ctidTraderAccountId`` is proto3-optional: unset reads back
    as ``0``, which is never a real cTrader account id, so ``0`` doubles as
    "not set" and the caller only compares when the result is non-zero.
    Non-positive values are clamped to ``0`` for the same reason
    ``_payload_ctid_trader_account_ids`` drops them: a negative id cannot be
    proven foreign, and treating it as a foreign account would silently drop
    (e.g.) a disconnect event that should tear the session down.
    """
    try:
        account_id = int(getattr(payload, "ctidTraderAccountId", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return account_id if account_id > 0 else 0


def _payload_ctid_trader_account_ids(payload: Any) -> tuple[int, ...]:
    """``ctidTraderAccountIds`` on an inbound payload, ``()`` when unset.

    ``ProtoOAAccountsTokenInvalidatedEvent`` carries a *repeated* account-id
    field, so the singular ``_payload_ctid_trader_account_id`` helper cannot
    read it. Non-positive entries are dropped for the same reason the
    singular helper treats ``0`` as unset: ``0`` is never a real cTrader
    account id. An empty tuple means the event names no account: it cannot be
    proven foreign, so callers treat it like an own-account event.
    """
    try:
        return tuple(
            account_id
            for account_id in (
                int(raw_id)
                for raw_id in (getattr(payload, "ctidTraderAccountIds", None) or ())
            )
            if account_id > 0
        )
    except (TypeError, ValueError):
        return ()


_CTRADER_AUTH_FAILURE_FALLBACK = "cTrader account authorization failed"

#: ``ProtoOAAccountType`` numbers -> lowercase names. Mirrored here so the
#: account-type read does not depend on the proto enum object being bound
#: (tests stub ``_fetch_trader`` without loading the openapi types).
_CTRADER_ACCOUNT_TYPES = {0: "hedged", 1: "netted", 2: "spread_betting"}

#: Seconds an unreadable ``account_type`` read is negatively cached. Every
#: runner pass on an ``internal_max_open_per_symbol > 1`` cTrader deployment
#: asks again; without the window an unreachable broker would cost a full
#: request timeout on every pass while the refusal already fails closed.
_ACCOUNT_TYPE_RETRY_SECONDS = 60.0


def _reason_to_text(reason: Any) -> str:
    if hasattr(reason, "getErrorMessage"):
        try:
            return str(reason.getErrorMessage())
        except Exception:  # except-kind: loop-survival
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
                # `msvcrt` is a Windows-only stdlib module (typeshed gates it on
                # `sys_platform == "win32"`), so mypy on the Linux CI runner cannot
                # see its members at all: `attr-defined` is about the platform, not
                # about a runtime value. The `os.name == "nt"` guard is what makes
                # this branch reachable only where the module exists.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                break
            except OSError:
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]  # Windows-only module, see the lock call above
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

    from twisted.internet import reactor as _reactor

    # `twisted.internet.reactor` is a *module* at import time and the installed
    # reactor *instance* at run time: `installReactor()` rebinds
    # sys.modules["twisted.internet.reactor"] to the instance, which is why
    # `__class__.__module__` names the reactor class below. The static type is
    # therefore the module, so `running`/`startRunning` are genuinely not on it.
    # Take it as Any instead of two `# type: ignore[attr-defined]` comments:
    # those are unused whenever Twisted's own (partial) types are absent, and
    # `warn_unused_ignores` turns an unused ignore into a failure - so the same
    # source would type-check in CI and fail in a dev-extra-only checkout.
    reactor: Any = _reactor

    reactor_module = reactor.__class__.__module__
    if "asyncioreactor" not in reactor_module:
        raise RuntimeError(
            f"Unsupported Twisted reactor already installed: {reactor_module}. "
            "Expected asyncio reactor."
        )
    if not reactor.running:
        reactor.startRunning(installSignalHandlers=False)


_CONNECTION_SCOPED_TCP_PROTOCOL: Any = None


def _connection_scoped_tcp_protocol() -> Any:
    """``TcpProtocol`` whose send queue is private to each connection.

    ctrader-open-api 0.9.2 declares ``_send_queue`` as a CLASS attribute on
    ``TcpProtocol`` (a ``deque`` that ``send()``/``_sendStrings()`` never
    rebind), so every protocol instance in the process drains ONE shared
    deque: whichever connection's ``LoopingCall`` pops an entry writes those
    bytes on ITS socket. ``_send_task`` and ``_lastSendMessageTime`` are also
    class attributes upstream, but they are only ever READ before being
    ASSIGNED per instance (``connectionMade``/``_sendStrings``), so they were
    already effectively per connection; re-declaring all three here is
    defensive and keeps the intent explicit if upstream changes that.
    The daemon runs one ``CTraderLiveAdapter`` per cTrader account -
    each with its own ``Client``/TCP connection but the same app credentials -
    so an ``ProtoOAAccountAuthReq`` queued by one adapter can be sent on
    another adapter's session. The broker then authorises the wrong account on
    each connection, the response resolves on the wrong ``Client``
    (``clientMsgId`` is only unique per client, so it is dropped), the real
    request times out, and exactly one account ends up authenticated per
    daemon start with the winner decided by queue ordering. Instantiating the
    three attributes per protocol gives each Twisted connection (one protocol
    instance per TCP connection) its own queue, send task and heartbeat clock.
    """
    global _CONNECTION_SCOPED_TCP_PROTOCOL
    if _CONNECTION_SCOPED_TCP_PROTOCOL is not None:
        return _CONNECTION_SCOPED_TCP_PROTOCOL
    from ctrader_open_api import TcpProtocol

    class _ConnectionScopedTcpProtocol(TcpProtocol):
        def __init__(self) -> None:
            super().__init__()
            self._send_queue: deque[Any] = deque()
            self._send_task = None
            self._lastSendMessageTime = None

    _CONNECTION_SCOPED_TCP_PROTOCOL = _ConnectionScopedTcpProtocol
    return _CONNECTION_SCOPED_TCP_PROTOCOL


class CTraderLiveAdapter(BaseLiveAdapter):
    """cTrader live adapter backed by OpenApiPy."""

    broker_name = "ctrader"
    # `supports_stop_amendment` (#168): a cTrader position's SL/TP is amended
    # in place through ProtoOAAmendPositionSLTPReq - the broker replaces the
    # protective levels atomically, so the previous stop keeps working until
    # the new one is accepted and stays if the request is refused. The adapter
    # confirms every amendment by re-reading the position from the broker
    # before reporting success; an unconfirmed outcome raises instead of
    # claiming the stop moved.

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
        self.accounts_env_path = resolve_accounts_env_path(
            accounts_env_path or None, default=DEFAULT_ACCOUNTS_ENV
        )
        raw_account_id = str(account_id or account_config_id).strip()
        self.ctid_trader_account_id = int(raw_account_id) if raw_account_id else 0
        # Presence is checked separately from the value on purpose. `resolve_env_value` returns
        # "" both for a key that is absent and for one an operator deliberately blanked, and it is
        # built to keep those apart - "present-but-empty stays empty". Passing its result through
        # `or None` collapsed the blanked case into "absent", so the strict parser never saw the
        # empty string and applied the default instead. preflight and daemon both refuse on
        # `CTRADER_DEMO=""`; this is the reader that opens the socket, and with `default=False`
        # the same blank would have selected live trading silently.
        demo_configured = (env_values is not None and "CTRADER_DEMO" in env_values) or (
            "CTRADER_DEMO" in os.environ
        )
        self.demo = (
            parse_bool_env(
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
        self._rebate_logged_deal_ids: set[int] = set()
        self._unresolved_execution_events: deque[Any] = deque()
        # Deal ids already flagged ledger_reconciliation_required this process.
        # The reconcile pass re-reads the same 7-day window on every connect and
        # every pending retry; without this each unattributable deal would write
        # a new audit row and a new ERROR alert per pass, drowning the next real
        # flag in repeats. Deliberately NOT cleared on disconnect: a reconnect
        # is exactly when the pass re-runs.
        self._flagged_broker_close_deals: set[int] = set()
        # One ERROR alert per process per coverage gap: the scan cursor
        # leaves the same uncovered range pending on every pass until it is
        # scanned, and alerting each time would drown the channel.
        self._broker_close_coverage_alert_sent = False
        self._broker_close_deal_list_min_interval_seconds = (
            BROKER_CLOSE_RECONCILE_DEAL_LIST_MIN_INTERVAL_SECONDS
        )
        self._reconciliation_required = False
        self._reconciliation_retry_at = 0.0
        self._connected = False
        self._authenticated = False
        self._connect_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        # The access token the CURRENT session authenticated with ("" when no
        # session is authenticated). ``ProtoOAAccountsTokenInvalidatedEvent``
        # names accounts, not tokens, so the invalidation handler compares
        # this against ``self.access_token`` to tell "our own refresh already
        # replaced the dead token" apart from "the session's token died".
        self._session_access_token = ""
        # ``time.monotonic()`` of the last token-pair rotation this adapter
        # knows about (own refresh or adoption of a sibling's persisted pair);
        # 0.0 when none happened this process.
        self._token_pair_rotated_at = 0.0
        # Depth counter: ``> 0`` from the moment an own token rotation
        # starts until its re-auth completes. A counter, not a bool, so two
        # concurrent auth-error rotations cannot clear each other's
        # in-flight marker. ``ProtoOAAccountsTokenInvalidatedEvent`` names
        # accounts, not tokens, so this explicit in-flight signal - not the
        # token comparison - is what protects the window between refresh
        # completion and the AccountAuthRes from a teardown.
        self._own_token_rotation_in_flight = 0
        # ``time.monotonic()`` of the last session teardown or transport
        # loss; ``listen_executions`` waits out
        # ``_LISTENER_RECONNECT_MIN_INTERVAL_SECONDS`` from it before
        # reconnecting so a flapping broker cannot spin the loop. This is
        # monotonic history for the process lifetime - never reset on
        # (re)connect, or the floor silently disappears.
        self._last_session_teardown_at = 0.0
        # Deferreds handed out by ``_client.send`` and still awaiting a
        # response. Upstream ``Client._disconnected`` clears
        # ``_responseDeferreds`` BEFORE our disconnect callback runs, so a
        # plain transport loss cannot enumerate them there - the adapter
        # tracks them itself to errback promptly instead of letting each
        # caller wait out its response timeout.
        self._in_flight_request_deferreds: set[Any] = set()
        # Bumped by every ``_fail_in_flight_requests`` run; ``_send_request``
        # compares it across the send→track window so a deferred created
        # while teardown was already failing the previous batch is failed
        # promptly instead of sitting out its response timeout.
        self._in_flight_fail_generation = 0
        self._symbol_name_to_id: dict[str, int] = {}
        # #125: pending get_quote calls by symbolId, and one lock per symbol
        # (cTrader allows a single spot subscription per symbol and session).
        self._spot_waiters: dict[int, _SpotWaiter] = {}
        self._spot_locks: dict[int, asyncio.Lock] = {}
        self.quote_timeout_seconds = 5.0
        # #80: broker-authoritative per-symbol scales (digits/lotSize), keyed
        # by upper symbol name. Tests/fallbacks use _fallback_symbol_scale.
        self._symbol_props: dict[str, SymbolScale] = {}
        # #146: (baseAssetId, quoteAssetId) per upper symbol name, from the
        # light symbol list, and the account's deposit asset. Session state.
        self._symbol_assets: dict[str, tuple[int, int]] = {}
        # #240: ProtoOASymbol.leverageId per upper symbol name, captured with
        # the symbol props; resolved to a leverage via GetDynamicLeverageByID.
        self._symbol_leverage_ids: dict[str, int] = {}
        self._deposit_asset_id: int | None = None
        self._asset_names: dict[int, str] = {}
        self._asset_names_task: asyncio.Task[dict[int, str]] | None = None
        self._position_ids_by_symbol: dict[str, tuple[int, ...]] = {}
        # Broker volume per open position id, refreshed by ``get_positions``
        # alongside ``_position_ids_by_symbol``. The exit-target resolver's
        # order cache uses it to prove its read is not stale: a cached order
        # list may never be the proof that pins a lot to a position when the
        # position's volume has moved since the fetch (#382 maintainer
        # decision - a netted book's opener set only grows).
        self._position_volumes: dict[int, int] = {}
        # #382: position id -> (monotonic timestamp, order list, volume at
        # fetch) for the exit-target resolver. A per-lot exit fan-out would
        # otherwise spend one broker round trip per open position *per exit*
        # inside the account lock; the TTL bounds staleness and the recorded
        # volume invalidates the entry the moment the position's size moves,
        # which is the only way a netted position's opener set can change.
        self._position_orders_cache: dict[int, tuple[float, Any, int | None]] = {}
        # ``ProtoOAAccountType`` name ("hedged"/"netted"/"spread_betting"),
        # cached after the first successful ``_fetch_trader`` read. An
        # unreadable read is negatively cached for
        # ``_ACCOUNT_TYPE_RETRY_SECONDS`` (monotonic timestamp below) so a
        # broker outage cannot stall every runner pass; "unknown" is never
        # pinned permanently - the next read after the window retries.
        self._account_type: str | None = None
        self._account_type_unreadable_at: float | None = None
        self._order_id_to_signal_id: dict[int, str] = {}
        # position_id -> {signal_id: monotonic timestamp}: a ClosePositionReq
        # has no clientOrderId of ours, so until its broker order id is known
        # a fill on the position is retained for reconciliation rather than
        # booked as a broker-side close (see _close_position_fill_pending).
        # Keyed per signal so two concurrent close submits on one position do
        # not clear each other's guard when the first finishes.
        self._pending_close_positions: dict[int, dict[str, float]] = {}
        # id()s of execution events parked in ``_unresolved_execution_events``
        # because their own close submit is still in flight. These are
        # requeued after a reconciliation pass WITHOUT spending the retry
        # budget: the pending mark can live up to the TTL, and a budgeted
        # requeue would crash-loop the listener for that whole window.
        self._pending_close_fill_event_ids: set[int] = set()
        # #80: order id -> upper symbol name (for order-only fills whose
        # payload carries no symbolId).
        self._order_id_to_symbol: dict[int, str] = {}
        self._terminal_auth_failure_reason = ""
        self._terminal_auth_failure_code = ""
        self._already_logged_in_notified = False
        # Payload types already reported by the foreign-account drop guard in
        # ``_on_message``; only SpotEvent is rate-limited this way (a steady
        # drip cannot flood), every other dropped payload is logged each time.
        self._dropped_foreign_account_payload_types: set[int] = set()
        # Monotonic per-adapter suffix plus a per-adapter random token for the
        # ``clientMsgId`` ``_send_request`` puts on the wire, so a NO RESPONSE
        # log line identifies the request and two adapters can never mint the
        # same id for the same request sequence.
        self._client_msg_seq = 0
        self._client_msg_token = uuid4().hex[:12]
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
        self._ProtoOAAccountDisconnectEvent: Any = None
        self._ProtoOAAccountsTokenInvalidatedEvent: Any = None
        self._ProtoOATraderReq: Any = None
        self._ProtoOATraderRes: Any = None
        self._ProtoOAOrderDetailsReq: Any = None
        self._ProtoOAOrderDetailsRes: Any = None
        self._ProtoOAOrderListReq: Any = None
        self._ProtoOAOrderListRes: Any = None
        self._ProtoOAOrderListByPositionIdReq: Any = None
        self._ProtoOAOrderListByPositionIdRes: Any = None
        self._ProtoOADealListReq: Any = None
        self._ProtoOADealListRes: Any = None
        self._ProtoOAReconcileReq: Any = None
        self._ProtoOAReconcileRes: Any = None
        self._ProtoOARefreshTokenReq: Any = None
        self._ProtoOARefreshTokenRes: Any = None
        self._ProtoOADealStatus: Any = None
        self._ProtoOAPositionStatus: Any = None
        self._ProtoOAOrderStatus: Any = None
        self._ProtoOAOrderType: Any = None
        self._ProtoOATradeSide: Any = None
        self._ProtoOAExecutionType: Any = None
        self._ProtoOATimeInForce: Any = None

        self._ProtoOASubscribeSpotsReq: Any = None
        self._ProtoOAUnsubscribeSpotsReq: Any = None
        self._ProtoOAGetDynamicLeverageByIDReq: Any = None
        self._ProtoOAGetDynamicLeverageByIDRes: Any = None
        self._ProtoOAGetPositionUnrealizedPnLReq: Any = None
        self._ProtoOAGetPositionUnrealizedPnLRes: Any = None

        self._ProtoOAGetTrendbarsReq: Any = None
        self._ProtoOAGetTrendbarsRes: Any = None
        self._ProtoOATrendbarPeriod: Any = None
        self._ProtoOAAmendPositionSLTPReq: Any = None

        self._heartbeat_payload_type: int | None = None
        self._spot_payload_type: int | None = None
        self._execution_payload_type: int | None = None
        self._error_payload_type: int | None = None
        self._order_error_payload_type: int | None = None
        self._account_disconnect_payload_type: int | None = None
        self._accounts_token_invalidated_payload_type: int | None = None

    def _load_openapi_types(self) -> None:
        if self._client_class is not None:
            return
        assert self._loop is not None
        _ensure_asyncio_twisted_reactor(self._loop)
        try:
            from ctrader_open_api import Client, EndPoints, Protobuf
            from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAAccountAuthReq,
                ProtoOAAccountAuthRes,
                ProtoOAAccountDisconnectEvent,
                ProtoOAAccountsTokenInvalidatedEvent,
                ProtoOAAmendPositionSLTPReq,
                ProtoOAApplicationAuthReq,
                ProtoOAApplicationAuthRes,
                ProtoOACancelOrderReq,
                ProtoOAClosePositionReq,
                ProtoOADealListReq,
                ProtoOADealListRes,
                ProtoOAErrorRes,
                ProtoOAExecutionEvent,
                ProtoOAGetDynamicLeverageByIDReq,
                ProtoOAGetDynamicLeverageByIDRes,
                ProtoOAGetPositionUnrealizedPnLReq,
                ProtoOAGetPositionUnrealizedPnLRes,
                ProtoOAGetTrendbarsReq,
                ProtoOAGetTrendbarsRes,
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
                ProtoOASpotEvent,
                ProtoOASubscribeSpotsReq,
                ProtoOASymbolsListReq,
                ProtoOASymbolsListRes,
                ProtoOATraderReq,
                ProtoOATraderRes,
                ProtoOAUnsubscribeSpotsReq,
            )
            from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
                ProtoOADealStatus,
                ProtoOAExecutionType,
                ProtoOAOrderStatus,
                ProtoOAOrderType,
                ProtoOAPositionStatus,
                ProtoOATimeInForce,
                ProtoOATradeSide,
                ProtoOATrendbarPeriod,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ctrader-open-api package is required for CTraderLiveAdapter."
            ) from exc

        self._client_class = Client
        self._TcpProtocol = _connection_scoped_tcp_protocol()
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
        self._ProtoOAAccountDisconnectEvent = ProtoOAAccountDisconnectEvent
        self._ProtoOAAccountsTokenInvalidatedEvent = ProtoOAAccountsTokenInvalidatedEvent
        self._ProtoOATraderReq = ProtoOATraderReq
        self._ProtoOATraderRes = ProtoOATraderRes
        self._ProtoOAOrderDetailsReq = ProtoOAOrderDetailsReq
        self._ProtoOAOrderDetailsRes = ProtoOAOrderDetailsRes
        self._ProtoOAOrderListReq = ProtoOAOrderListReq
        self._ProtoOAOrderListRes = ProtoOAOrderListRes
        self._ProtoOAGetDynamicLeverageByIDReq = ProtoOAGetDynamicLeverageByIDReq
        self._ProtoOAGetDynamicLeverageByIDRes = ProtoOAGetDynamicLeverageByIDRes
        self._ProtoOAGetPositionUnrealizedPnLReq = ProtoOAGetPositionUnrealizedPnLReq
        self._ProtoOAGetPositionUnrealizedPnLRes = ProtoOAGetPositionUnrealizedPnLRes
        self._ProtoOAOrderListByPositionIdReq = ProtoOAOrderListByPositionIdReq
        self._ProtoOAOrderListByPositionIdRes = ProtoOAOrderListByPositionIdRes
        self._ProtoOADealListReq = ProtoOADealListReq
        self._ProtoOADealListRes = ProtoOADealListRes
        self._ProtoOAReconcileReq = ProtoOAReconcileReq
        self._ProtoOAReconcileRes = ProtoOAReconcileRes
        self._ProtoOARefreshTokenReq = ProtoOARefreshTokenReq
        self._ProtoOARefreshTokenRes = ProtoOARefreshTokenRes
        self._ProtoOADealStatus = ProtoOADealStatus
        self._ProtoOAPositionStatus = ProtoOAPositionStatus
        self._ProtoOAOrderStatus = ProtoOAOrderStatus
        self._ProtoOAOrderType = ProtoOAOrderType
        self._ProtoOATradeSide = ProtoOATradeSide
        self._ProtoOAExecutionType = ProtoOAExecutionType
        self._ProtoOATimeInForce = ProtoOATimeInForce

        self._ProtoOASubscribeSpotsReq = ProtoOASubscribeSpotsReq
        self._ProtoOAUnsubscribeSpotsReq = ProtoOAUnsubscribeSpotsReq
        self._ProtoOAGetTrendbarsReq = ProtoOAGetTrendbarsReq
        self._ProtoOAGetTrendbarsRes = ProtoOAGetTrendbarsRes
        self._ProtoOAAmendPositionSLTPReq = ProtoOAAmendPositionSLTPReq
        self._ProtoOATrendbarPeriod = ProtoOATrendbarPeriod

        self._heartbeat_payload_type = ProtoHeartbeatEvent().payloadType
        self._spot_payload_type = ProtoOASpotEvent().payloadType
        self._execution_payload_type = ProtoOAExecutionEvent().payloadType
        self._error_payload_type = ProtoOAErrorRes().payloadType
        self._order_error_payload_type = ProtoOAOrderErrorEvent().payloadType
        self._account_disconnect_payload_type = ProtoOAAccountDisconnectEvent().payloadType
        self._accounts_token_invalidated_payload_type = (
            ProtoOAAccountsTokenInvalidatedEvent().payloadType
        )

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

    def _note_session_access_token(self, access_token: str | None = None) -> None:
        """Record the access token the current session authenticated with.

        ``access_token`` overrides the recorded value: callers that just
        confirmed an ``AccountAuthRes`` pass the token the request actually
        carried, so a sibling-pair adoption during the flight cannot make
        the record describe a token the session never authenticated with.
        """
        self._session_access_token = (
            access_token if access_token is not None else self.access_token
        )

    def _clear_session_rotation_state(self) -> None:
        """Drop the recorded session token and any in-flight rotation marker.

        Every session-death path (``_teardown_client``, ``_on_disconnected``,
        ``disconnect``) must call this: a stale ``_session_access_token`` or
        a latched ``_own_token_rotation_in_flight`` would let
        ``_token_invalidation_skip_reason`` treat a genuine invalidation as
        our own rotation echo and never tear the dead session down. The
        invalidation handler also self-heals a latched flag on a dead
        session as a backstop for any future path that forgets.
        """
        self._session_access_token = ""
        self._own_token_rotation_in_flight = 0

    def _self_heal_rotation_marker_on_dead_session(self) -> None:
        """Clear stale session-rotation state on a dead session.

        ``_session_access_token`` and ``_own_token_rotation_in_flight`` only
        mean something while a session is connected: no live session can be
        mid-rotation, and the recorded token describes a session that no
        longer exists. A session-death path that forgets
        ``_clear_session_rotation_state`` leaves both behind, which would
        let ``_token_invalidation_skip_reason`` treat a genuine invalidation
        as our own rotation echo (latched counter, or the grace branch on a
        stale session token) and never tear the session down. Kept out of
        the skip-reason check itself so that check stays pure.
        """
        if not self._connected and (
            self._own_token_rotation_in_flight or self._session_access_token
        ):
            self._clear_session_rotation_state()

    def _adopt_persisted_token_pair(self) -> bool:
        """Adopt the token pair persisted in the accounts env file when it differs.

        Every cTrader adapter in the process shares ONE
        ``CTRADER_ACCESS_TOKEN``/``CTRADER_REFRESH_TOKEN`` pair, and a cTrader
        refresh rotates BOTH tokens and invalidates the old ones - so a
        sibling adapter's refresh leaves this adapter's in-memory copy dead.
        The accounts env file is the only place the rotated pair survives
        (file-first credential precedence), so it is re-read here rather than
        trusting memory. Token values are never logged.

        Returns True when a different complete pair was adopted; the caller
        then knows the previously held pair was stale and must not spend its
        (possibly already-consumed) refresh token.
        """
        try:
            file_values = read_accounts_env_file(self.accounts_env_path)
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Could not re-read cTrader accounts env file for token adoption",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "env_path": str(self.accounts_env_path),
                        "error": str(exc),
                    }
                },
            )
            return False
        file_access = str(file_values.get("CTRADER_ACCESS_TOKEN") or "").strip()
        file_refresh = str(file_values.get("CTRADER_REFRESH_TOKEN") or "").strip()
        if not file_access or not file_refresh:
            return False
        if file_access == self.access_token and file_refresh == self.refresh_token:
            return False
        self.access_token = file_access
        self.refresh_token = file_refresh
        self._token_pair_rotated_at = time.monotonic()
        logger.info(
            "Adopted newer persisted cTrader token pair",
            extra={
                "data": {
                    "account_id": self.ctid_trader_account_id,
                    "account_config_id": self.account_config_id,
                    "env_path": str(self.accounts_env_path),
                }
            },
        )
        return True

    def _token_rotation_in_flight(self) -> bool:
        """A token refresh or own rotation+re-auth is in flight here.

        ``_refresh_lock`` covers the refresh itself; the lock is released
        before the re-auth await, which ``_own_token_rotation_in_flight``
        (a depth counter) spans instead.
        """
        return self._refresh_lock.locked() or (
            self._own_token_rotation_in_flight > 0
        )

    def _token_invalidation_skip_reason(self) -> str | None:
        """Why a token-invalidation event must NOT tear down this session.

        ``ProtoOAAccountsTokenInvalidatedEvent`` names accounts, not tokens,
        and cTrader emits it for EVERY token rotation - including the
        ``_refresh_access_token`` this adapter (or a sibling sharing the same
        persisted pair) just performed and the re-auth that followed. Tearing
        down a session the refresh path already re-authenticated - or is
        re-authenticating right now - would turn a recoverable submit into an
        unknown outcome, so the teardown is skipped when:

        - a refresh is in flight on this adapter (``_refresh_lock`` held);
        - this adapter's own token rotation is still being re-authenticated
          (``_own_token_rotation_in_flight``: a depth counter incremented
          when the refresh starts and decremented when the rotation's
          re-auth completes - this covers the gap between refresh
          completion and re-auth that a token comparison cannot);
        - the session already re-authenticated on the current access token
          and a token-pair rotation completed within
          ``_TOKEN_INVALIDATION_OWN_ROTATION_GRACE_SECONDS`` (a late event
          for the OLD token arriving after our re-auth).

        A session token that differs from the current access token is NOT a
        skip reason: it means the session authenticated with a token that a
        rotation has since replaced, so the session is dead (a sibling's
        rotation terminates every account session on the pair) and must be
        torn down so the next ``connect()`` re-authenticates.
        """
        if self._token_rotation_in_flight():
            if self._refresh_lock.locked():
                return "a token refresh is in flight on this adapter"
            return (
                "this adapter's own token rotation is being "
                "re-authenticated"
            )
        rotated_at = self._token_pair_rotated_at
        if (
            self._session_access_token
            and self.access_token == self._session_access_token
            and rotated_at
            and (
                time.monotonic() - rotated_at
                < _TOKEN_INVALIDATION_OWN_ROTATION_GRACE_SECONDS
            )
        ):
            return "a token rotation completed moments ago"
        return None

    async def _refresh_access_token(self, *, failed_access_token: str | None = None) -> bool:
        """Refresh cTrader OAuth tokens via Open API and persist them safely."""
        if not self.refresh_token or self._ProtoOARefreshTokenReq is None:
            logger.warning(
                "cTrader access token refresh unavailable: missing refresh token or OpenAPI type",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            return False
        # Defensive: the adapter holds an access token the session never
        # authenticated with and no OTHER own rotation is in flight - the
        # session is dead (a sibling rotated the shared pair, or our own
        # earlier rotation's re-auth failed after its refresh succeeded),
        # not the token. The caller re-authenticates with the adopted pair;
        # spending the shared single-use refresh token here would rotate
        # the pair AGAIN and invalidate every sibling account on it.
        # ``<= 1`` because the production caller
        # (``_rotate_token_pair_and_reauthenticate``) increments
        # ``_own_token_rotation_in_flight`` BEFORE calling this, so "no own
        # rotation in flight" can only mean "no rotation but this one" -
        # a plain truthiness check would make this guard dead on the real
        # path. A count above 1 means a concurrent rotation is also in
        # flight; it adopts-or-refreshes inside the lock below instead.
        if (
            self._session_access_token
            and self.access_token != self._session_access_token
            and self._own_token_rotation_in_flight <= 1
        ):
            # Adopt before re-authenticating: a sibling may have persisted
            # an even newer pair since this adapter's in-memory copy, and
            # re-authenticating with a stale access token would just fail
            # again. Read-only - no refresh token is spent either way.
            self._adopt_persisted_token_pair()
            return True
        refresh_token_before_lock = self.refresh_token
        # All cTrader adapters share one persisted token pair and a cTrader
        # refresh token is single-use: the per-file lock serializes refreshes
        # so two adapters never spend the same refresh token concurrently.
        async with _shared_token_refresh_lock(
            self.accounts_env_path
        ), self._refresh_lock:
            # A sibling adapter may already have rotated the shared pair;
            # the persisted file is the freshest truth. Adopting it IS the
            # refresh outcome the callers need (they re-authenticate with
            # ``self.access_token``), and it must happen BEFORE spending a
            # refresh token that may already have been consumed.
            if self._adopt_persisted_token_pair():
                return True
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
            except Exception as exc:  # except-kind: money-decision
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
            self._token_pair_rotated_at = time.monotonic()
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
        # A connect event describes a socket that has not been authenticated yet, so the
        # per-connection auth state starts clean here. Disconnect and teardown both clear these
        # flags already; what neither covers is a transport that reconnects without reporting the
        # disconnect. Leaving `_authenticated` True would let `_authenticate` return before
        # sending a request - it short-circuits on `_authenticated and _symbol_name_to_id` - and
        # every `not self._authenticated` gate, including the order paths, would then pass on a
        # connection the broker never authenticated.
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
        # The session is dead with the transport: drop the recorded session
        # token and any in-flight rotation marker so a stale value cannot
        # make a later invalidation event look like our own rotation echo.
        # (An in-flight rotation cannot survive this anyway - its pending
        # send is errbacked by ``_fail_in_flight_requests`` below and the
        # helper's ``finally`` clears the flag on the way out.)
        self._clear_session_rotation_state()
        self._last_session_teardown_at = time.monotonic()
        # Upstream ``Client._disconnected`` clears ``_responseDeferreds``
        # BEFORE invoking this callback, so the dict enumeration in
        # ``_fail_in_flight_requests`` finds nothing - the adapter-side
        # ``_in_flight_request_deferreds`` set is what still names the
        # pending requests. Without the errback each in-flight caller would
        # wait out its own response timeout on a socket that is already gone;
        # the error is a connection-lost, which every caller classifies as
        # an UNKNOWN outcome - never as not-submitted.
        self._fail_in_flight_requests()
        # Executions may have been missed while the transport was down; flag
        # broker reconciliation and wake the listener now rather than after
        # its 30 s queue timeout, same as a session teardown.
        self._flag_reconciliation_and_wake_listener()
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
        except Exception as exc:  # except-kind: money-decision
            if payload_type == self._execution_payload_type:
                self._reconciliation_required = True
                self._reconciliation_retry_at = 0.0
                with contextlib.suppress(asyncio.QueueFull):
                    self._execution_queue.put_nowait(_EXECUTION_RECONCILE_WAKE)
            logger.warning("Could not decode cTrader payload", extra={"data": {"error": str(exc)}})
            return

        if payload_type in self._account_scoped_payload_types():
            payload_account_id = _payload_ctid_trader_account_id(payload)
            if payload_account_id and payload_account_id != self.ctid_trader_account_id:
                self._drop_foreign_account_payload(
                    payload_type, payload, payload_account_id
                )
                return

        # Server-initiated lifecycle events are not in
        # ``_account_scoped_payload_types``: they do their own account check
        # (the invalidation event carries a repeated id list) and log foreign
        # events at DEBUG rather than the WARNING drop path.
        if (
            self._account_disconnect_payload_type is not None
            and payload_type == self._account_disconnect_payload_type
        ):
            self._handle_account_disconnect_event(payload)
            return

        if (
            self._accounts_token_invalidated_payload_type is not None
            and payload_type == self._accounts_token_invalidated_payload_type
        ):
            self._handle_accounts_token_invalidated_event(payload)
            return

        if self._spot_payload_type is not None and payload_type == self._spot_payload_type:
            self._handle_spot_event(payload)
            return

        if payload_type == self._execution_payload_type:
            try:
                self._execution_queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._reconciliation_required = True
                with contextlib.suppress(asyncio.QueueFull):
                    self._execution_queue.put_nowait(_EXECUTION_RECONCILE_WAKE)
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

    def _handle_account_disconnect_event(self, payload: Any) -> None:
        """Server ended this account's session (``ProtoOAAccountDisconnectEvent``).

        Transient by contract: the event carries no terminal reason, so the
        session state is dropped and the ordinary ``connect()`` path - with
        its existing retry backoff - re-authenticates on next use. No
        terminal auth latch is set. Foreign-account events are ignored at
        DEBUG: a lifecycle event for another account says nothing about this
        session.
        """
        payload_account_id = _payload_ctid_trader_account_id(payload)
        if payload_account_id and payload_account_id != self.ctid_trader_account_id:
            logger.debug(
                "Ignoring cTrader account-disconnect event for a different account",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "payload_account_id": payload_account_id,
                    }
                },
            )
            return
        logger.warning(
            "cTrader account disconnect event: the broker ended this account's "
            "session; re-authenticating on the next connect",
            extra={
                "data": {
                    "account_id": self.ctid_trader_account_id,
                    "account_config_id": self.account_config_id,
                    "payload_account_id": payload_account_id,
                }
            },
        )
        self._teardown_client()

    def _handle_accounts_token_invalidated_event(self, payload: Any) -> None:
        """Server invalidated this account's access token
        (``ProtoOAAccountsTokenInvalidatedEvent``).

        The event names accounts, not tokens, and cTrader emits it for EVERY
        token rotation - including the ``_refresh_access_token`` this adapter
        just performed on this same connection. Tearing down a session the
        refresh path already re-authenticated (or is re-authenticating right
        now) would turn a recoverable submit into an unknown outcome, so the
        teardown is skipped when ``_token_invalidation_skip_reason`` finds
        the event is the echo of a rotation we already handled. Otherwise
        the dead token must never carry another order: the session is torn
        down and the next ``connect()`` re-authenticates.

        Sibling caveat: every cTrader adapter shares ONE persisted token
        pair, and a refresh by adapter A invalidates the tokens adapter B
        holds in memory AND terminates B's account session. B cannot heal
        that by spending the shared refresh token again - it is
        single-use and may already be consumed, so B would rotate the pair
        a second time and invalidate every sibling in turn. Instead B
        adopts the persisted pair here (``_adopt_persisted_token_pair``)
        and drops the dead session; the next ``connect()``
        re-authenticates with the adopted access token through
        ``_send_account_auth_with_recovery`` and spends no refresh token.
        """
        account_ids = _payload_ctid_trader_account_ids(payload)
        if account_ids and self.ctid_trader_account_id not in account_ids:
            logger.debug(
                "Ignoring cTrader token-invalidation event for different accounts",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "payload_account_ids": list(account_ids),
                    }
                },
            )
            return
        reason = str(getattr(payload, "reason", "") or "").strip()
        # A sibling adapter may already have rotated the shared pair; adopt
        # the persisted pair BEFORE deciding, so the token comparison below
        # sees the freshest credentials. Skipped while our own rotation is
        # in flight - ``_refresh_lock`` held during the refresh, or
        # ``_own_token_rotation_in_flight`` during the re-auth await (the
        # lock is already released by then): mutating the pair mid-rotation
        # could clobber the credentials the in-flight AccountAuthReq carries
        # and the rotation the refresh is about to persist, and the event is
        # our own rotation echo anyway (``_token_invalidation_skip_reason``
        # skips it either way).
        self._self_heal_rotation_marker_on_dead_session()
        if not self._token_rotation_in_flight():
            self._adopt_persisted_token_pair()
        skip_reason = self._token_invalidation_skip_reason()
        if skip_reason is not None:
            logger.info(
                "cTrader accounts token invalidated event ignored: %s",
                skip_reason,
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "account_config_id": self.account_config_id,
                        "reason": reason,
                        "payload_account_ids": list(account_ids),
                    }
                },
            )
            return
        logger.warning(
            "cTrader accounts token invalidated event (reason: %s); "
            "dropping the session so the next connect refreshes the token "
            "and re-authenticates",
            reason or "not given",
            extra={
                "data": {
                    "account_id": self.ctid_trader_account_id,
                    "account_config_id": self.account_config_id,
                    "reason": reason,
                    "payload_account_ids": list(account_ids),
                }
            },
        )
        self._teardown_client()

    def _account_scoped_payload_types(self) -> frozenset[int]:
        """Inbound payload types that name a ``ctidTraderAccountId``.

        These are the payloads the foreign-account drop guard in
        ``_on_message`` covers (spot quotes, executions, order errors, error
        responses); the server lifecycle events do their own account check
        with DEBUG-level foreign logging. A payload of any other type is
        ignored here anyway. Computed per message because tests bind the
        ``_*_payload_type`` attributes directly without
        ``_load_openapi_types``.
        """
        return frozenset(
            payload_type
            for payload_type in (
                self._spot_payload_type,
                self._execution_payload_type,
                self._error_payload_type,
                self._order_error_payload_type,
            )
            if payload_type is not None
        )

    def _drop_foreign_account_payload(
        self, payload_type: int, payload: Any, payload_account_id: int
    ) -> None:
        """Drop an event/error that names a different cTrader account.

        The per-connection send queue removed the known way one account's
        ``AccountAuthReq`` could land on another account's socket, but nothing
        else stops a misrouted ``ExecutionEvent``/``SpotEvent``/``ErrorRes``
        from being queued, persisted or latched under THIS account's
        ``account_config_id``. The payload is dropped, not re-routed: the
        owning adapter's connection never sees it either way, and booking it
        here would misattribute the fill.

        ``SpotEvent`` is the only type logged once per adapter: quotes arrive
        on every tick, so a steady drip of foreign quotes would flood the
        log. A dropped ``ExecutionEvent``/``OrderErrorEvent``/``ErrorRes`` is
        a real fill, rejection or error on another account that nobody here
        will book, so every one is logged at WARNING with its order/deal ids
        for manual reconciliation.
        """
        is_spot = (
            self._spot_payload_type is not None
            and payload_type == self._spot_payload_type
        )
        if is_spot and payload_type in self._dropped_foreign_account_payload_types:
            return
        if is_spot:
            self._dropped_foreign_account_payload_types.add(payload_type)
        data: dict[str, Any] = {
            "account_id": self.ctid_trader_account_id,
            "payload_account_id": payload_account_id,
            "payload_type": payload_type,
            "payload_class": type(payload).__name__,
        }
        # ``ExecutionEvent`` nests its ids under ``order``/``deal``/``position``
        # submessages; collect whichever ids the payload actually carries so
        # the drop can be reconciled against the owning account's records.
        sources = [payload] + [
            getattr(payload, name, None) for name in ("order", "deal", "position")
        ]
        for source in sources:
            if source is None:
                continue
            for name in (
                "orderId",
                "dealId",
                "positionId",
                "symbolId",
                "executionType",
                "errorCode",
            ):
                if name in data:
                    continue
                value = getattr(source, name, None)
                if value not in (None, "", 0):
                    data[name] = value
        logger.warning(
            "Dropping cTrader payload for a different account",
            extra={"data": data},
        )

    async def _send_request(self, request: Any, *, timeout_seconds: int = 10) -> Any:
        if self._client is None or self._loop is None:
            raise RuntimeError("cTrader client is not initialized.")
        # An explicit clientMsgId (upstream would use ``str(id(deferred))``)
        # makes the id self-describing and lets a NO RESPONSE log line name
        # the request that was lost instead of only its account. The id is
        # account-scoped AND process-unique: upstream resolves pending
        # Deferreds by clientMsgId alone, so ``<PayloadType>-<seq>`` would let
        # one adapter's response satisfy another adapter's identical pending
        # request if a payload is ever misrouted again.
        self._client_msg_seq += 1
        client_msg_id = (
            f"{self.ctid_trader_account_id}-{self._client_msg_token}-"
            f"{type(request).__name__}-{self._client_msg_seq}"
        )
        client = self._client
        # Capture BEFORE send: moving this after ``client.send`` would miss a
        # ``_fail_in_flight_requests`` that ran during the send, leaving the
        # new deferred untracked until its response timeout.
        fail_generation = self._in_flight_fail_generation
        deferred = client.send(
            request,
            clientMsgId=client_msg_id,
            responseTimeoutInSeconds=timeout_seconds,
        )
        self._in_flight_request_deferreds.add(deferred)
        if self._in_flight_fail_generation != fail_generation:
            # ``_fail_in_flight_requests`` ran between send() returning and
            # the deferred being tracked: it can no longer see this deferred,
            # which would sit until its own response timeout. Fail it now.
            self._in_flight_request_deferreds.discard(deferred)
            errback = getattr(deferred, "errback", None)
            if callable(errback):
                with contextlib.suppress(Exception):
                    errback(
                        CTraderConnectionLostError(
                            "cTrader connection lost before the response arrived"
                        )
                    )
            raise CTraderConnectionLostError(
                "cTrader connection lost before the response arrived"
            )
        try:
            response_message = await deferred.asFuture(self._loop)
        except Exception as exc:  # except-kind: money-decision
            request_exc: Any = exc
            with contextlib.suppress(AttributeError):
                request_exc.ctrader_request_payload_type = type(request).__name__
                request_exc.ctrader_client_msg_id = client_msg_id
            # A request timeout is logged HERE, at the point it happens, for
            # every request type - orders included - not only when a connect
            # retry finally gives up and ``_log_connect_failure`` runs.
            is_response_timeout, timeout_s = _classify_deferred_response_timeout(
                exc, str(exc)
            )
            if is_response_timeout:
                logger.warning(
                    "cTrader request timed out with no response",
                    extra={
                        "data": {
                            "account_id": self.ctid_trader_account_id,
                            "request_payload_type": type(request).__name__,
                            "client_msg_id": client_msg_id,
                            "response_timeout_s": timeout_s,
                        }
                    },
                )
            raise
        finally:
            self._in_flight_request_deferreds.discard(deferred)
        payload = self._Protobuf.extract(response_message)
        # Upstream resolves the pending Deferred by clientMsgId alone; the
        # ``_on_message`` foreign-account guard never sees this path. A
        # response naming a different account must never be booked here.
        payload_account_id = _payload_ctid_trader_account_id(payload)
        if payload_account_id and payload_account_id != self.ctid_trader_account_id:
            logger.error(
                "Dropping cTrader response for a different account",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "payload_account_id": payload_account_id,
                        "payload_class": type(payload).__name__,
                        "client_msg_id": client_msg_id,
                    }
                },
            )
            raise RuntimeError(
                f"cTrader response for foreign account {payload_account_id} "
                f"on adapter for account {self.ctid_trader_account_id}; "
                "refusing to use it."
            )
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
            if not _is_terminal_auth_error(error_code, error_text):
                raise
            if not await self._rotate_token_pair_and_reauthenticate(
                failed_access_token=access_token_before_send
            ):
                raise
            if (
                self._ProtoOAAccountAuthReq is not None
                and self._ProtoOAAccountAuthRes is not None
            ):
                self._authenticated = True
            return await self._send_request_with_refresh_retry(
                request, timeout_seconds=timeout_seconds, _depth=_depth + 1
            )

    async def _rotate_token_pair_and_reauthenticate(
        self, *, failed_access_token: str | None = None
    ) -> bool:
        """Refresh-or-adopt the shared token pair and re-authenticate the account.

        Returns True when the pair is usable: either the account re-auth
        request was confirmed (``_note_session_access_token`` records the
        token the request actually carried), or the OpenAPI auth types are
        not loaded and the caller retries the original request. Returns
        False when no usable pair could be produced; whatever the refresh
        or re-auth raised propagates.

        ``_own_token_rotation_in_flight`` spans the whole refresh+re-auth
        window - incremented here and decremented unconditionally by the
        ``finally``. It is a depth counter, not a bool: two requests can hit
        an auth error concurrently and run overlapping rotations, and a
        bool would let the first ``finally`` clear the second rotation's
        marker. While the count is non-zero a
        ``ProtoOAAccountsTokenInvalidatedEvent`` landing anywhere inside the
        window (including between refresh completion and the AccountAuthRes)
        is recognised as our own rotation echo, not a fresh invalidation.
        The ``finally`` is deliberate: a count left set makes
        ``_token_invalidation_skip_reason`` ignore every later invalidation
        event, so a dead session would never be torn down.
        """
        self._own_token_rotation_in_flight += 1
        try:
            refreshed = await self._refresh_access_token(
                failed_access_token=failed_access_token
            )
            if not refreshed:
                return False
            if (
                self._ProtoOAAccountAuthReq is None
                or self._ProtoOAAccountAuthRes is None
            ):
                # No re-auth will run, so nothing will note the new session
                # token; the flag must not outlive this rotation attempt.
                return True
            # The token the AccountAuthReq carries is fixed at send time; a
            # sibling-pair adoption during the flight must not make the
            # session record describe a token the session never
            # authenticated with.
            auth_token = self.access_token
            account_auth_res = await self._send_account_auth_request(
                timeout_seconds=15
            )
            if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
                raise RuntimeError(
                    "Unexpected account auth response after refresh: "
                    f"{type(account_auth_res).__name__}"
                )
            self._note_session_access_token(auth_token)
            return True
        finally:
            # Clamped: a session-death path may have zeroed the counter
            # while this rotation was still unwinding.
            self._own_token_rotation_in_flight = max(
                0, self._own_token_rotation_in_flight - 1
            )

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
        auth_token = self.access_token
        try:
            account_auth_res = await self._send_account_auth_request(timeout_seconds=15)
        except CTraderApiError as exc:
            await self._recover_account_auth_failure(exc)
        else:
            if not isinstance(account_auth_res, self._ProtoOAAccountAuthRes):
                raise RuntimeError(
                    f"Unexpected account auth response: {type(account_auth_res).__name__}"
                )
            self._note_session_access_token(auth_token)

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
        if not await self._rotate_token_pair_and_reauthenticate():
            reason = exc.description or error_text or _CTRADER_AUTH_FAILURE_FALLBACK
            self._mark_terminal_auth_failure(reason, error_code=error_code)
            raise CTraderAuthTerminalError(reason, error_code=error_code) from exc

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

    async def resolve_exit_position_target(
        self, symbol: str, entry_signal_id: str
    ) -> int | None:
        """The open position ``entry_signal_id``'s order opened (#382).

        Each candidate position's orders are read and the opening order's
        ``clientOrderId`` (the persisted signal id, account-validated by
        :meth:`_signal_id_for_position_order`) decides the match. ``None``
        means no open position resolves to that entry - the caller then
        fails closed on a multi-position book. An unreadable order list or a
        signal matching several positions raises: guessing which lot a close
        belongs to is exactly what this method exists to prevent.
        """
        upper = str(symbol or "").strip().upper()
        position_ids = self._position_ids_by_symbol.get(upper, ())
        if not position_ids:
            return None
        wanted = str(entry_signal_id or "").strip()
        if not wanted:
            return None
        matches = await self._entry_matching_position_ids(position_ids, wanted, upper)
        if len(matches) > 1:
            raise ValueError(
                f"cTrader entry {wanted} matches several open positions on "
                f"{upper} ({matches}); refusing to guess the exit target."
            )
        return matches[0] if matches else None

    async def _entry_matching_position_ids(
        self, position_ids: tuple[int, ...], wanted: str, upper: str
    ) -> list[int]:
        """The positions whose opening orders belong exclusively to ``wanted``.

        A position resolves only when its distinct opening-order identities
        are exactly ``{wanted}``: every non-closing order must resolve to that
        one entry signal, and no other opener - a sibling lot's signal, a
        manual order, an exit signal this build cannot attribute - may share
        the position (#382 maintainer decision). On a netted book several
        entries merge into one position; matching it for one lot would let a
        broker-sized full exit close the siblings' quantity too, so a shared
        position returns no match and the exit sizes off the lot residual
        instead. On a hedging book each position has exactly one opening
        order, so the rule is a no-op there.
        """
        matches: list[int] = []
        for position_id in position_ids:
            orders = await self._position_orders_for_exit_target(position_id)
            if orders is None:
                raise ValueError(
                    f"cTrader position {position_id} order list is unreadable; "
                    "cannot resolve the exit target for "
                    f"{wanted} on {upper}."
                )
            openers: set[str] = set()
            for index, order in enumerate(getattr(orders, "order", []) or []):
                if bool(getattr(order, "closingOrder", False)):
                    continue
                signal_id = await self._signal_id_for_position_order(order)
                if signal_id:
                    openers.add(signal_id)
                else:
                    # An opener this build cannot attribute still counts: it
                    # is proof the position is NOT exclusively ``wanted``'s.
                    openers.add(f"__unresolved_opener__{index}")
            if openers == {wanted}:
                matches.append(position_id)
        return matches

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

    def _order_type_name(self, order: Any) -> str | None:
        """The order's type name as the broker stated it (#134), or ``None``.

        ``orderType`` is the protobuf enum's int value; the adapter's
        ``ProtoOAOrderType`` wrapper names it. ``None`` when the payload or
        the enum wrapper does not state one - never a fabricated type.
        """
        if order is None:
            return None
        raw = getattr(order, "orderType", None)
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        name_fn = getattr(self._ProtoOAOrderType, "Name", None)
        if not callable(name_fn):
            return None
        return _enum_name_or_none(name_fn, value)

    def _requested_order_qty(self, order: Any, symbol: str) -> float | None:
        """The order's requested volume converted to qty units (#134).

        ``ProtoOAOrder`` carries no root ``volume``: the requested volume lives
        in ``order.tradeData.volume`` in broker volume units, converted with
        the same symbol scale the fill uses. ``None`` when the order is absent
        or states no positive volume - never an invented quantity.
        """
        if order is None:
            return None
        trade_data = getattr(order, "tradeData", None)
        volume = _to_float(getattr(trade_data, "volume", 0), default=0.0)
        if volume <= 0.0:
            return None
        return _broker_volume_to_qty(volume, self._scale_for(symbol).volume_per_qty)

    async def _load_symbols_into_cache(self) -> None:
        symbols_req = self._ProtoOASymbolsListReq()
        symbols_req.ctidTraderAccountId = self.ctid_trader_account_id
        symbols_req.includeArchivedSymbols = False
        symbols_res = await self._send_request_with_refresh_retry(symbols_req, timeout_seconds=15)
        if not isinstance(symbols_res, self._ProtoOASymbolsListRes):
            raise RuntimeError(f"Unexpected symbols response: {type(symbols_res).__name__}")

        symbol_name_to_id: dict[str, int] = {}
        symbol_assets: dict[str, tuple[int, int]] = {}
        for symbol in symbols_res.symbol:
            name = str(symbol.symbolName).upper()
            symbol_name_to_id[name] = int(symbol.symbolId)
            assets = _light_symbol_assets(symbol)
            if assets is not None:
                symbol_assets[name] = assets
        self._symbol_name_to_id = symbol_name_to_id
        self._symbol_assets = symbol_assets
        # #80: broker-API symbol properties (digits/lotSize/minVolume) are the
        # source of truth for per-symbol scales. Required: a failed or
        # unexpected detail fetch aborts authentication rather than letting
        # _scale_for fall back to static scales that can misstate size.
        await self._load_symbol_props_into_cache(symbol_name_to_id)
        self._authenticated = True
        # Covers the ALREADY_LOGGED_IN path too: no AccountAuthRes was
        # confirmed there, so the current access token is the best record of
        # what the session runs on (a wrong guess only errs toward teardown).
        self._note_session_access_token()

    async def _load_symbol_props_into_cache(self, symbol_name_to_id: dict[str, int]) -> None:
        """Fetch full ProtoOASymbol properties per symbol id (#80).

        ProtoOALightSymbol (SymbolsList) exposes no digits/lotSize — those
        live on ProtoOASymbol via ProtoOASymbolByIdReq. Verified against the
        installed ctrader_open_api descriptors before touching this path:
        digits/pipPosition required, lotSize/min/max/stepVolume optional.
        """
        if not symbol_name_to_id:
            self._symbol_props.clear()
            self._symbol_leverage_ids.clear()
            return
        for name in symbol_name_to_id:
            self._symbol_props.pop(name, None)
            self._symbol_leverage_ids.pop(name, None)
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
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "cTrader symbol-props fetch failed; refusing fallback scales",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "error": str(exc),
                    }
                },
            )
            raise
        if not isinstance(detail_res, ProtoOASymbolByIdRes):
            raise RuntimeError(
                f"Unexpected symbol-props response: {type(detail_res).__name__}; "
                "refusing to authenticate with fallback scales."
            )
        # Archived entries first so live properties always win on collision.
        for detail in list(getattr(detail_res, "archivedSymbol", [])) + list(detail_res.symbol):
            try:
                sid = int(detail.symbolId)
            except (TypeError, ValueError):
                continue
            name = id_to_name.get(sid)  # type: ignore[assignment]  # name reused with a wider type across branches
            if name is None:
                continue
            # #240: the symbol's dynamic-leverage id rides on the same detail
            # payload. Archived entries come first, so a live entry's id wins.
            leverage_id = _optional_int(getattr(detail, "leverageId", None))
            if leverage_id is not None and leverage_id > 0:
                self._symbol_leverage_ids[name] = leverage_id
            else:
                self._symbol_leverage_ids.pop(name, None)
            fallback = _fallback_symbol_scale(name) if _fallback_scale_class(name) else None
            scale = _symbol_scale_from_broker_props(
                digits=getattr(detail, "digits", None),
                lot_size=getattr(detail, "lotSize", None),
                min_volume=getattr(detail, "minVolume", None),
                fallback=fallback,
                step_volume=getattr(detail, "stepVolume", None),
            )
            if scale is not None:
                self._symbol_props[name] = scale
            self._persist_symbol_venue_calendar(detail, name)

    def _persist_symbol_venue_calendar(self, detail: Any, name: str) -> None:
        """Cache ProtoOASymbol schedule/timezone/holidays as a venue calendar.

        The same ProtoOASymbolById response already carries the broker's
        session schedule; persisting it lets coverage gates and live checks
        share one authoritative calendar (#133). Best-effort per symbol.
        """
        try:
            schedule = list(getattr(detail, "schedule", []) or [])
            holidays = list(getattr(detail, "holiday", []) or [])
            if not schedule and not holidays:
                return
            timezone_name = str(getattr(detail, "scheduleTimeZone", "") or "")
            if not timezone_name.strip():
                # Schedule seconds are relative to the broker zone; caching
                # them as UTC would misplace every session boundary, so an
                # absent timezone means no cache — the venue stays unresolved
                # and coverage fails closed.
                return
            payload = venue_calendar.ctrader_schedule_payload(
                symbol=name,
                schedule=schedule,
                schedule_timezone=timezone_name,
                holidays=holidays,
            )
            venue_calendar.save_cached_calendar(
                data_dir=DATA_DIR, source="ctrader", symbol=name, payload=payload
            )
        except Exception:  # except-kind: loop-survival
            logger.warning(
                "cTrader venue-calendar cache write failed for %s",
                name,
                exc_info=True,
            )

    def _extract_order_id_from_payload(self, payload: Any) -> int | None:
        if isinstance(payload, self._ProtoOAExecutionEvent):
            if payload.HasField("order") and payload.order.orderId > 0:
                return int(payload.order.orderId)
            if payload.HasField("deal") and payload.deal.orderId > 0:
                return int(payload.deal.orderId)
        if hasattr(payload, "orderId") and int(getattr(payload, "orderId", 0)) > 0:
            return int(payload.orderId)
        return None

    def exit_qty_step(self, symbol: str) -> float:
        """The symbol's volume step in qty units: what an exit is floored to."""
        scale = self._scale_for(symbol)
        return float(max(int(scale.step_volume), 1)) / float(scale.volume_per_qty)

    @staticmethod
    def _exit_volume(symbol: str, qty: float, scale: SymbolScale) -> int:
        """An exit's broker volume, rounded DOWN to the step (#121 review).

        ``round()`` (the entry conversion) could close up to half a volume unit
        more than the fraction asked for. Raises :class:`ExitBelowStepError`
        when less than one step remains.
        """
        step_qty = float(max(int(scale.step_volume), 1)) / float(scale.volume_per_qty)
        floored_qty = floor_qty_to_step(qty, step_qty)
        if floored_qty <= 0.0:
            raise ExitBelowStepError(
                f"cTrader exit for {symbol} ignored: the requested qty {qty:g} is below "
                f"the volume step ({step_qty:g} qty = {scale.step_volume} volume units).",
                requested_qty=qty,
                qty_step=step_qty,
            )
        # A whole number of steps: round() on a float returns an int, and this
        # only removes float noise.
        return round(floored_qty * float(scale.volume_per_qty))

    def _verify_sizing_scale(
        self, *, symbol: str, metadata: Mapping[str, Any], scale: SymbolScale
    ) -> None:
        """The #146 guard: refuse a qty sized against a scale that moved.

        The daemon sizes this qty with a pip value derived from the symbol
        scale at sizing time and records it as
        ``pip_value_units_per_qty``. If the scale changed since (e.g. a
        reconnect whose props fetch failed fell back to the static table), the
        same qty would be a different position size, so the submit fails closed.
        A caller pip value carries no units-per-qty, so the check cannot run
        for it: converting the sized qty with whatever scale is current could
        be a different position size (#146), and that fails closed too.

        Called from :meth:`_resolve_order_volume` and again by ``submit_order``
        immediately before the request is built, because a symbol scale can
        move across that submit's awaits (a reconnect's props refresh inside
        ``_resolve_symbol_id``) and the resolved volume is pinned, not
        re-resolved, against the new scale.
        """
        sized_units = metadata.get("pip_value_units_per_qty")
        if sized_units is None and metadata.get("pip_value_source") == "caller":
            # An explicit caller pip value carries no units-per-qty, so the
            # submit-time scale check cannot run; converting the sized qty with
            # whatever scale is current could be a different position size
            # (#146). Fail closed instead of skipping the check.
            raise ValueError(
                f"cTrader cannot verify the volume scale for {symbol}: the "
                "caller-supplied pip value carries no pip_value_units_per_qty; "
                "refusing to submit a qty whose sizing scale is unknown."
            )
        if sized_units is None:
            return
        current_units = scale.volume_per_qty / CTRADER_VOLUME_PER_BASE_UNIT
        try:
            sized_units_value = float(sized_units)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"Invalid pip_value_units_per_qty for {symbol}: {sized_units!r}"
            ) from exc
        if not math.isclose(sized_units_value, current_units, rel_tol=1e-9, abs_tol=0.0):
            raise ValueError(
                f"cTrader volume scale for {symbol} changed since sizing "
                f"({sized_units_value!r} -> {current_units!r} units per qty); "
                "refusing to submit a qty sized for a different contract."
            )

    def _resolve_order_volume(self, signal: LiveSignal) -> int:
        """Resolve the binding risk-engine ``qty`` into broker volume units.

        Returns a whole number of broker volume units as a builtin ``int`` -
        the request seams are typed to that and refuse anything else, so a
        refactor returning a float would fail closed at the builders rather
        than change a size.

        Reads only the sizing metadata (``qty``, the pip value and
        ``risk_sizing_mode``). In particular it reads no position or exit
        metadata, which is why ``submit_order`` does not re-resolve the exit
        path after it records the broker position id: a resolver that starts
        reading that metadata must restore the re-resolution (and its
        idempotency compare) with it - see
        ``test_ctrader_resolved_volume_ignores_exit_position_metadata``.
        """
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
        self._verify_sizing_scale(
            symbol=signal.symbol, metadata=metadata, scale=scale
        )
        if str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE:
            volume = self._exit_volume(signal.symbol, qty, scale)
        else:
            # Entries: floor onto the broker grid (never exceed the risk budget).
            volume = _floor_to_volume_step(
                _qty_to_broker_volume(qty, scale.volume_per_qty), scale.step_volume
            )
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
                except Exception as exc:  # except-kind: money-decision
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
        except Exception as exc:  # except-kind: money-decision
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
        raise exc

    def _log_connect_failure(
        self, exc: Exception, error_text: str, message: str
    ) -> None:
        # A response timeout is checked FIRST, and as its own case, because it is categorically
        # not a credentials problem: the socket is up and the request was written to it. Routing
        # it into the token branch is how an operator ends up refreshing a credential that works.
        is_response_timeout, response_timeout_s = _classify_deferred_response_timeout(
            exc, error_text
        )
        if is_response_timeout:
            waited = (
                f"within {response_timeout_s}s"
                if response_timeout_s is not None
                else "within the configured response timeout"
            )
            timeout_data: dict[str, Any] = {
                "account_id": self.ctid_trader_account_id,
                "error": error_text,
                "response_timeout_s": response_timeout_s,
            }
            # ``_send_request`` stamps the request that was lost on the
            # exception; without it a timed-out order request is
            # indistinguishable from a timed-out auth request in the logs.
            request_payload_type = getattr(exc, "ctrader_request_payload_type", None)
            if request_payload_type is not None:
                timeout_data["request_payload_type"] = request_payload_type
            client_msg_id = getattr(exc, "ctrader_client_msg_id", None)
            if client_msg_id is not None:
                timeout_data["client_msg_id"] = client_msg_id
            logger.info(
                "cTrader request written but NO RESPONSE %s; the transport is up, so this is "
                "not a credentials problem and not a TLS handshake failure. The request reached "
                "the socket and nothing came back.",
                waited,
                extra={"data": timeout_data},
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
            # A fallback fires precisely when the cause could not be classified, so it must not
            # name one. This message used to say "connect/auth unavailable", which pointed an
            # operator at a token that was demonstrably fine; the `error` field carries the real
            # text, so nothing is lost by saying so.
            logger.info(
                "cTrader connect failed; cause NOT CLASSIFIED - read the error field rather than "
                "assuming credentials. daemon will retry",
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

    def _fail_in_flight_requests(self, client: Any | None = None) -> None:
        """Errback every pending request deferred.

        Sources: the adapter-side ``_in_flight_request_deferreds`` set (which
        survives upstream's ``_responseDeferreds.clear()`` on disconnect) and,
        when ``client`` is given and not yet cleared, the client's
        ``_responseDeferreds`` dict. Upstream ``Client._disconnected`` only
        ``clear()``s ``_responseDeferreds`` without errbacking them, so each
        in-flight ``_send_request`` would otherwise sit until its own
        ``addTimeout`` fires (up to the request timeout) even though the
        socket is already gone. Failing them promptly frees callers sooner;
        the error is a connection-lost, which every caller classifies as an
        UNKNOWN outcome (the request may already have been written) - never
        as not-submitted and never as a broker refusal.
        """
        self._in_flight_fail_generation += 1
        pending: list[Any] = []
        seen: set[int] = set()
        adapter_pending = list(self._in_flight_request_deferreds)
        self._in_flight_request_deferreds.clear()
        client_pending: list[Any] = []
        if client is not None:
            try:
                deferreds = getattr(client, "_responseDeferreds", None)
                if deferreds is None:
                    # The attribute itself is gone (ctrader-open-api bump):
                    # adapter-tracked deferreds are still errbacked below,
                    # but the miss must not be silent.
                    logger.warning(
                        "cTrader client exposes no _responseDeferreds; "
                        "only adapter-tracked in-flight requests are failed",
                        extra={"data": {"account_id": self.ctid_trader_account_id}},
                    )
                elif deferreds:
                    # Tolerate a non-mapping shape (list/set) in case a
                    # ctrader-open-api bump changes the container type.
                    values = (
                        deferreds.values()
                        if hasattr(deferreds, "values")
                        else deferreds
                    )
                    client_pending = list(values)
            except Exception as exc:  # except-kind: loop-survival
                # A ctrader-open-api bump that renames or reshapes
                # ``_responseDeferreds`` must not abort the rest of teardown
                # (``stopService``, the event clear and the listener wake),
                # but the missed errbacks must not be silent either.
                logger.warning(
                    "Could not enumerate in-flight cTrader requests during teardown",
                    extra={"data": {"error": str(exc)}},
                )
        for deferred in adapter_pending + client_pending:
            if id(deferred) in seen:
                continue
            seen.add(id(deferred))
            pending.append(deferred)
        if not pending:
            return
        errback_failures = 0
        for deferred in pending:
            if getattr(deferred, "called", False):
                # Already resolved (response arrived or timed out); errback
                # would raise AlreadyCalledError.
                continue
            try:
                deferred.errback(
                    CTraderConnectionLostError(
                        "cTrader connection lost before the response arrived"
                    )
                )
            except Exception:  # except-kind: loop-survival
                errback_failures += 1
        if errback_failures:
            logger.warning(
                "Could not errback %d in-flight cTrader request(s) during "
                "teardown; their awaiters will hit their own timeout",
                errback_failures,
            )

    def _teardown_client(self) -> None:
        """Drop the current client and ask Twisted to stop its service so a later
        connect() attempt can build a fresh one. Safe to call when no client exists.
        """
        client = self._client
        self._client = None
        self._connected = False
        self._authenticated = False
        self._already_logged_in_notified = False
        self._clear_session_rotation_state()
        self._last_session_teardown_at = time.monotonic()
        if client is not None:
            self._fail_in_flight_requests(client)
            try:
                stop = getattr(client, "stopService", None)
                if stop is not None:
                    stop()
            except Exception:  # except-kind: loop-survival
                pass
        try:
            self._connected_event.clear()
        except Exception:  # except-kind: loop-survival
            pass
        self._flag_reconciliation_and_wake_listener()

    def _flag_reconciliation_and_wake_listener(self) -> None:
        """Flag broker reconciliation and wake ``listen_executions`` now.

        The teardown/disconnect wakes the listener so the gap is reconciled
        immediately - it would otherwise sleep out its 30 s ``queue.get``
        timeout before reconnecting and reconciling. If the queue is full
        the wake is dropped, so reconciliation is flagged explicitly. Any
        stale retry backoff is cleared too, or the flagged reconcile would
        wait out a prior failure's ``_reconciliation_retry_at``.
        """
        self._reconciliation_required = True
        self._reconciliation_retry_at = 0.0
        try:
            self._execution_queue.put_nowait(_EXECUTION_RECONCILE_WAKE)
        except asyncio.QueueFull:
            logger.warning(
                "cTrader execution queue full on session loss; "
                "listener wake dropped, reconciliation already flagged",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )

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
        # #144: broker-side position closes have no signal of ours, so the
        # per-signal pass above can never see them; the account deal list can.
        # The scan floor is the oldest ledger lot still holding quantity: a
        # close can only belong to a position the ledger still holds, so flat
        # history must not widen the scan. The flat 7-day window this replaced
        # let a close older than a week - a daemon down that long, or a
        # position held for months - keep the ledger permanently mismatched
        # (the 2026-11-24 soak's stale EURUSD lot).
        open_lots = await asyncio.to_thread(
            list_live_account_open_position_lots, self.account_config_id
        )
        recovered += await self._reconcile_broker_side_closes(
            not_before_ms=self._oldest_open_exposure_ms(open_lots),
            open_lots=open_lots,
        )
        return recovered

    @staticmethod
    def _oldest_open_exposure_ms(open_lots: list[dict[str, Any]]) -> int | None:
        """The oldest open ledger lot's timestamp in ms, or ``None``."""
        timestamps = [
            timestamp
            for timestamp in (_lot_timestamp_ms(lot) for lot in open_lots)
            if timestamp is not None
        ]
        return min(timestamps) if timestamps else None

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
        if not metadata or not is_exit_only_signal_metadata(metadata):
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
        if position_id is not None:
            # The close's broker order id is known now: fills on the position
            # can attribute to this exit signal, so the pending mark has done
            # its job (an unknown-outcome submit may have left it behind).
            self._clear_pending_close_mark(position_id, signal_id)
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

    def _reconciled_deal_symbol(
        self, deal: Any, *, broker_order_id: str, order: Any | None
    ) -> str | None:
        """The symbol a reconciled deal trades, or ``None`` when it cannot be determined."""
        deal_symbol_id = int(getattr(deal, "symbolId", 0) or 0)
        if not deal_symbol_id and order is not None:
            deal_symbol_id = int(getattr(order, "symbolId", 0) or 0)
        deal_symbol = self._symbol_name_for_id(deal_symbol_id) if deal_symbol_id else None
        if deal_symbol is None:
            try:
                deal_symbol = self._order_id_to_symbol.get(int(broker_order_id))
            except (TypeError, ValueError):
                deal_symbol = None
        return deal_symbol

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
        deal_symbol = self._reconciled_deal_symbol(
            deal, broker_order_id=broker_order_id, order=order
        )
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
            # The same guarded resolver and the same presence verdict as the live fill path:
            # this one read the raw field, so a NaN or a negative price from a reconciled deal
            # was stored as-is while the stream path refused it.
            actual_price=self._resolve_execution_price(order=order, deal=deal),
            actual_price_observed=self._execution_price_presence(order=order, deal=deal),
            filled_qty=filled_qty,
            commission=_deal_commission(
                deal,
                # ``getattr``: tests build partial adapters via ``__new__`` without
                # ``__init__``; a missing set only means the ERROR is not deduped.
                rebate_logged=getattr(self, "_rebate_logged_deal_ids", None),
            ),
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=broker_order_id,
            dedupe_key=f"ctrader:deal:{deal_id}",
            order_type=self._order_type_name(order),
            requested_qty=self._requested_order_qty(order, deal_symbol),
            # The deal id is positive here - this path returns early when `deal_id <= 0` - so the
            # stored value is the broker's real id, not a sentinel.
            broker_fill_id=_deal_id_or_none(deal),
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
            # Same semantics as the stream path: when the order states its cumulative executed
            # volume, persist only what that cumulative still lacks. Plain `save_live_execution`
            # treats a repeated dedupe key as a duplicate, so a deal that was first stored with a
            # smaller volume and has since been amended upward would never grow here - and a deal
            # skipped by the stream's cumulative cap (out-of-order delivery) is healed here
            # because the order's *final* cumulative leaves room for it.
            cumulative_qty = 0.0
            cumulative_volume = self._resolve_order_cumulative_volume(details_order)
            if cumulative_volume > 0:
                deal_symbol = self._reconciled_deal_symbol(
                    deal, broker_order_id=broker_order_id, order=details_order
                )
                if deal_symbol is not None:
                    cumulative_qty = _broker_volume_to_qty(
                        cumulative_volume, self._scale_for(deal_symbol).volume_per_qty
                    )
            if cumulative_qty > 0:
                persisted = (
                    await asyncio.to_thread(
                        save_live_execution_up_to_cumulative, execution, cumulative_qty
                    )
                    > 0.0
                )
            else:
                persisted = await asyncio.to_thread(save_live_execution, execution)
            if persisted:
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
        self._clear_session_rotation_state()
        self._symbol_name_to_id.clear()
        self._order_id_to_signal_id.clear()
        self._pending_close_positions.clear()
        self._position_ids_by_symbol.clear()
        self._position_volumes.clear()
        self._position_orders_cache.clear()
        # #80: props and order-symbol mappings are session state — a
        # reconnect must re-fetch broker props, never trade on stale scales,
        # and the order map must not grow unboundedly across sessions.
        self._symbol_props.clear()
        self._symbol_leverage_ids.clear()
        self._symbol_assets.clear()
        self._deposit_asset_id = None
        self._asset_names.clear()
        if self._asset_names_task is not None:
            self._asset_names_task.cancel()
            self._asset_names_task = None
        self._order_id_to_symbol.clear()
        # The queue swap and clears below discard unprocessed execution
        # events, stored unresolved events and any pending reconcile wake;
        # if any existed, flag broker reconciliation so the next listener
        # run reconciles the gap instead of losing the fills silently.
        had_pending_execution_work = (
            not self._execution_queue.empty()
            or bool(self._unresolved_execution_events)
        )
        self._execution_queue = asyncio.Queue(maxsize=5000)
        self._execution_retry_counts.clear()
        self._unresolved_execution_events.clear()
        if had_pending_execution_work:
            self._reconciliation_required = True
            self._reconciliation_retry_at = 0.0
        self._pending_close_fill_event_ids.clear()
        self._terminal_auth_failure_reason = ""
        self._terminal_auth_failure_code = ""
        # A deliberate disconnect leaves the same in-flight requests pending
        # as a transport loss: fail them now instead of letting each caller
        # wait out its response timeout. ``_on_disconnected`` cannot cover
        # this path - ``self._client`` is already None here, so its
        # identity check returns early.
        self._fail_in_flight_requests(client)

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
        # Everything up to and including the request build is provably before
        # the broker sees an order, so a failure here is marked and the caller
        # can safely retry instead of treating the outcome as unknown. The
        # `_submit_new_order_request` call below is deliberately outside.
        close_position_id: int | None = None
        with order_not_submitted():
            self._raise_if_terminal_auth_failed()
            signal_id = str(signal.id)
            existing_order_id = await asyncio.to_thread(
                get_live_signal_broker_order_id, signal_id
            )
            if existing_order_id:
                normalized_order_id = self._coerce_broker_order_id(existing_order_id)
                if normalized_order_id is None:
                    raise RuntimeError(
                        "Persisted cTrader broker order id is invalid; "
                        "refusing duplicate submission."
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
            exit_only = (
                str((signal.metadata or {}).get("risk_sizing_mode") or "")
                == EXIT_ONLY_RISK_SIZING_MODE
            )
            # Resolve the broker volume ONCE, here, from the binding qty, and
            # hand this value to the request builders below. They must not
            # resolve again: submit_order awaits several times after this
            # point, and a resolution that reads the scale fresh could size
            # the wire request off a scale that moved in the meantime - a
            # volume the persisted qty below no longer describes.
            order_volume = self._resolve_order_volume(signal)
            # Persist the quantity the broker is actually asked for, not the
            # unrounded risk-sized one: the volume grid floor can drop a
            # sub-step remainder the broker can never work, and a persisted
            # requested qty larger than the fill reads as a working-order
            # expectation forever in broker reconciliation. Normalizing from
            # the volume resolved above keeps the persisted qty and the wire
            # request on one number.
            scale = self._scale_for(signal.symbol)
            submitted_qty = _broker_volume_to_qty(order_volume, scale.volume_per_qty)
            # Only the lot-grid noise band (<= 1e-6 of a step, see
            # floor_qty_to_step/_qty_to_broker_volume) may push the normalized
            # qty an ulp above the binding qty; anything more means volume
            # resolution rounded up. The allowance is absolute qty units
            # scaled to the grid step - a relative tolerance would hide a
            # small overshoot on a large qty. The band tracks the shared
            # lot-grid floor's own noise constant (2x for float margin) so
            # the two cannot drift apart.
            step_qty = float(max(int(scale.step_volume), 1)) / float(
                scale.volume_per_qty
            )
            if submitted_qty > qty_value and not math.isclose(
                submitted_qty,
                qty_value,
                rel_tol=0.0,
                abs_tol=2.0 * float(STEP_NOISE) * step_qty,
            ):
                # Volume resolution floors, so this can only happen if a
                # future change rounds up: persisting an oversized qty would
                # both place an oversized order and hide the excess from
                # reconciliation. Fail closed instead.
                raise ValueError(
                    f"cTrader normalized qty {submitted_qty!r} exceeds the "
                    f"binding qty {qty_value!r} for {signal.symbol}; refusing "
                    "to submit an order larger than the risk-sized quantity."
                )
            signal = signal.model_copy(
                update={
                    "metadata": {
                        **(signal.metadata or {}),
                        "qty": submitted_qty,
                        "explicit_qty": submitted_qty,
                    }
                }
            )
            signal = signal.model_copy(
                update={
                    "metadata": {
                        key: value
                        for key, value in (signal.metadata or {}).items()
                        if key not in {"ctrader_volume", "volume"}
                    }
                }
            )
            # The ONE deliberate re-resolution: the signal's input CHANGED
            # since the call above - its qty is now the volume-grid quantity
            # the broker can work, not the binding qty - so resolving again is
            # not a duplicate, it is the guard that the persisted quantity
            # still resolves to the volume we are about to send. The float
            # round-trip (volume -> qty -> volume) stays far inside the
            # lot-grid floor's 1e-6-unit noise band for every volume a broker
            # int32 can express (~5e-7 units at INT32_MAX), so an exact int
            # compare cannot false-positive.
            reresolved_volume = self._resolve_order_volume(signal)
            if reresolved_volume != order_volume:
                raise RuntimeError(
                    "cTrader volume resolution is not idempotent under qty "
                    f"normalization for {signal.symbol}: resolved "
                    f"{order_volume} from the binding qty but "
                    f"{reresolved_volume} from the normalized qty; "
                    "refusing to submit."
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
                requested_position_id = self._requested_exit_position_id(
                    signal.metadata or {}
                )
                position_id = self._position_id_for_exit(
                    signal.symbol, requested_position_id
                )
                close_position_id = position_id
                # Records which broker position this close addresses. There is
                # deliberately no re-resolution here: _resolve_order_volume reads
                # neither position_id nor any other exit metadata, so this key
                # cannot change the volume - the volume resolved above stays the
                # one to send. If the resolver ever starts reading position or
                # exit metadata, the re-resolution must come back with it.
                signal = signal.model_copy(
                    update={
                        "metadata": {
                            **(signal.metadata or {}),
                            "position_id": position_id,
                        }
                    }
                )

            # Build the broker request BEFORE persisting: a refusal here
            # (unknown symbol, missing SL/TP) must not leave a live_signals
            # row that reads as "an order may exist" and routes retries into
            # the persisted-signal recovery path.
            symbol_id = await self._resolve_symbol_id(signal.symbol)
            # #146: the request builders used to re-resolve the volume, which
            # re-ran the "scale changed since sizing" check after these awaits -
            # so a symbol scale that moved in this window (a reconnect's props
            # refresh inside ``_resolve_symbol_id``) refused the submit. The
            # volume is now resolved once and passed down, so the check is
            # re-run here instead, against the scale as it stands right before
            # the request is built and before anything is persisted.
            self._verify_sizing_scale(
                symbol=signal.symbol,
                metadata=signal.metadata or {},
                scale=self._scale_for(signal.symbol),
            )
            if exit_only:
                if position_id is None:
                    raise RuntimeError("cTrader exit-only position id was not resolved.")
                request = self._build_close_position_request(
                    position_id=position_id,
                    volume=order_volume,
                )
            else:
                request = self._build_new_order_request(
                    signal, symbol_id=symbol_id, volume=order_volume
                )

            canonical_signal_id, created = await asyncio.to_thread(
                save_live_signal, signal
            )
            if not created:
                return await self._recover_duplicate_signal_order_id(canonical_signal_id)
        # #134: bracket the broker call with the order-timing stamps, persisted
        # with the order id once the broker answers.
        submitted_at = datetime.now(timezone.utc)
        if close_position_id is not None:
            # A ClosePositionReq carries no clientOrderId of ours: until the
            # broker order id is known, a fill on this position can only be
            # retained for reconciliation, not attributed - mark the position
            # so the listener does not book our own exit as a broker-side
            # close. The mark is per signal so a second concurrent close on
            # the same position keeps its own guard, and is lifted once this
            # submit's ownership is recorded below, or expires on its own
            # after an unknown-outcome submit.
            self._pending_close_positions.setdefault(close_position_id, {})[
                canonical_signal_id
            ] = time.monotonic()
        try:
            response = await self._submit_new_order_request(request)
        except BrokerOrderRejectedError:
            # A definitive refusal created no order: nothing can fill, so the
            # pending mark has nothing left to cover.
            if close_position_id is not None:
                self._clear_pending_close_mark(close_position_id, canonical_signal_id)
            raise
        acknowledged_at = datetime.now(timezone.utc)

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
        # Ownership is registered BEFORE the persist await: a fill event the
        # listener processes while the DB write is in flight still resolves to
        # this signal. A persist failure rolls the registration back; the
        # pending mark stays so fills keep being retained until reconciliation
        # recovers the broker order id.
        self._order_id_to_signal_id[order_id] = canonical_signal_id
        # #80: remember the symbol for order-only fills (no symbolId).
        self._order_id_to_symbol[order_id] = str(signal.symbol).strip().upper()
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id,
            canonical_signal_id,
            broker_order_id,
            submitted_at=submitted_at,
            acknowledged_at=acknowledged_at,
        )
        if not persisted:
            self._order_id_to_signal_id.pop(order_id, None)
            self._order_id_to_symbol.pop(order_id, None)
            raise RuntimeError("Failed to persist cTrader broker order id.")
        if close_position_id is not None:
            self._clear_pending_close_mark(close_position_id, canonical_signal_id)

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
        if position_id is not None:
            self._clear_pending_close_mark(position_id, signal_id)
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
            position_id = await self._persisted_exit_position_id(canonical_signal_id)
            if position_id is not None:
                # The close's broker order id is already known, so a pending
                # mark this exit left on its position has done its job (same
                # lift as the order-id recovery paths).
                self._clear_pending_close_mark(position_id, canonical_signal_id)
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

    async def _resolve_symbol_id(self, symbol: str) -> int:
        """Resolve a symbol to its broker id, raising when it is unknown.

        The return is declared ``int``, never ``int | None``: both lookup
        misses raise, so a ``None`` return would be unrepresentable.
        """
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
        """Build the position-addressed close for an already-resolved *volume*.

        ``submit_order`` resolves the volume exactly once and passes it here,
        exactly as it does for the entry request.
        """
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
        broker_volume = _validate_broker_volume(
            volume, field_name="cTrader close volume"
        )
        request = self._ProtoOAClosePositionReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = position_id_value
        request.volume = broker_volume
        return request

    def _build_new_order_request(
        self, signal: LiveSignal, *, symbol_id: int, volume: int
    ) -> Any:
        """Build the entry request for an already-resolved broker *volume*.

        ``submit_order`` resolves the volume exactly once (from the binding
        qty, then guards the normalized one) and passes it in: the request must
        carry that volume, not a fresh resolution against a scale that may have
        moved across the submit's awaits.

        ``volume`` is therefore required and must never gain a default that
        resolves it here - that would reintroduce exactly the divergence the
        single resolution removed, and the resolver-call-count spy tests are
        what catch it.
        """
        if signal.action not in {"buy", "sell"}:
            raise ValueError(f"Unsupported signal action: {signal.action}")
        metadata = signal.metadata or {}
        if str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE:
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
        # The volume no longer arrives validated from the resolver, so validate
        # it here, on the same contract the close request uses: whole broker
        # volume units or nothing.
        request.volume = _validate_broker_volume(
            volume, field_name="cTrader new-order volume"
        )
        request.timeInForce = self._ProtoOATimeInForce.IMMEDIATE_OR_CANCEL
        request.clientOrderId = str(signal.id)
        # One allowlist and one resolver for both levels (G19). ANY_FLOAT keeps
        # this adapter's own leniency: it refuses non-finite and non-positive
        # levels itself, in _resolve_relative_sl_tp, after the missing-level
        # guard below.
        stop_loss = resolve_signal_level(
            metadata, SL_METADATA_KEYS, parsing=LevelParsing.ANY_FLOAT
        ).price
        take_profit = resolve_signal_level(
            metadata, TP_METADATA_KEYS, parsing=LevelParsing.ANY_FLOAT
        ).price
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
        if relative_take is not None and relative_take > 0:
            # proto2 optional int64: assigning 0 would make the field PRESENT
            # and the broker validates a present relativeTakeProfit as > 0
            # ("SL/TP have to be positive"). "No take profit" means the field
            # is absent, so it is only ever assigned a positive distance.
            request.relativeTakeProfit = relative_take
        return request

    def _resolve_relative_sl_tp(
        self, signal: LiveSignal, stop_loss: float, take_profit: float | None
    ) -> tuple[int, int | None]:
        """Convert absolute SL/TP prices into cTrader relative units.

        Per the cTrader Open API docs, ProtoOANewOrderReq's absolute
        stopLoss/takeProfit fields are "Not supported for MARKET orders" — this
        adapter submits MARKET/IMMEDIATE_OR_CANCEL orders, so the distances are
        sent via relativeStopLoss/relativeTakeProfit instead: int64 in
        1/100000 of a price unit (123000 means 1.23), where for BUY
        stopLoss = entryPrice - relativeStopLoss and takeProfit
        = entryPrice + relativeTakeProfit (reversed for SELL).

        The broker also rejects a distance finer than the symbol's price precision
        (``Relative stop loss has invalid precision``), so each distance is floored
        to the symbol grid of ``10 ** (5 - price_digits)`` units (1 when the symbol
        is finer than 5 digits), i.e. toward the entry, so the real risk never
        exceeds the sized risk (docs/live-risk-consistency.md). A take-profit under
        one tick is sent as one tick; a stop-loss under one tick is refused,
        because widening it would exceed the sized risk.

        Returns ``(relative_stop, relative_take)`` where ``relative_take`` is
        ``None`` for the explicit unbounded-TP policy: the caller must leave
        ``relativeTakeProfit`` unset on the wire, because a present zero is a
        broker rejection, not "no take profit".
        """
        reference_price = float(signal.theoretical_price)
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError(
                f"Invalid reference price for {signal.symbol}: "
                f"{signal.theoretical_price!r}; refusing to submit."
            )
        symbol_scale = self._scale_for(signal.symbol)
        scale = CTRADER_RELATIVE_PRICE_UNIT_DIVISOR
        # The broker validates a relative distance against the symbol's own price
        # precision (``Relative stop loss has invalid precision``): on a 2-digit symbol
        # (ETHUSD, XAUUSD, indices) it must be a multiple of 1000 protocol units, on a
        # 3-digit one a multiple of 100. Sent unrounded, every such order is rejected.
        grid = 10 ** max(0, _power_of_ten_exponent(scale) - symbol_scale.price_digits)

        def distance(price: float, *, is_stop: bool) -> int:
            return _relative_distance(
                signal.symbol, reference_price, price, scale, grid, is_stop=is_stop
            )

        buy = signal.action == "buy"
        stop_outward = reference_price - stop_loss if buy else stop_loss - reference_price
        if stop_outward <= 0:
            raise ValueError(
                f"SL price is on the wrong side of the reference price for "
                f"{signal.symbol} ({signal.action}): stop_loss={stop_loss!r}, "
                f"reference={reference_price!r}; refusing to submit."
            )
        if take_profit is None:
            # C1: explicit unbounded policy — stop-only order. cTrader has no
            # "no take profit" value for a present field: the field must be
            # left unset entirely (None here -> the caller never assigns it).
            return (distance(stop_loss, is_stop=True), None)
        take_outward = take_profit - reference_price if buy else reference_price - take_profit
        if take_outward <= 0:
            raise ValueError(
                f"TP price is on the wrong side of the reference price for "
                f"{signal.symbol} ({signal.action}): take_profit={take_profit!r}, "
                f"reference={reference_price!r}; refusing to submit."
            )
        return (distance(stop_loss, is_stop=True), distance(take_profit, is_stop=False))

    async def _submit_new_order_request(self, request: Any) -> Any:
        try:
            response = await self._send_request_with_refresh_retry(request, timeout_seconds=20)
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        self._raise_for_refused_submit_response(response, request)
        return response

    @staticmethod
    def _classify_and_raise_refusal(
        message: str, *, error_code: str, definitive: bool
    ) -> None:
        """A refusal is ``BrokerOrderRejectedError`` only when definitive."""
        if not definitive:
            raise RuntimeError(message)
        raise BrokerOrderRejectedError(message, error_code=error_code)

    def _raise_for_refused_submit_response(self, response: Any, request: Any) -> None:
        """Raise for a correlated refusal payload; return for anything else.

        cTrader answers a refused NewOrderReq/ClosePositionReq either with a
        ProtoOAOrderErrorEvent or with a ProtoOAExecutionEvent whose
        executionType is ORDER_REJECTED; both are matched to THIS request by
        clientMsgId. Timeouts and transport failures never reach here: the
        request may have landed, so they stay ordinary exceptions (unknown
        outcome).
        """
        if self._ProtoOAOrderErrorEvent is not None and isinstance(
            response, self._ProtoOAOrderErrorEvent
        ):
            # Only an error event with no broker order id is a clean refusal:
            # a non-zero orderId means the broker created an order record, and
            # for a new-order request a non-zero positionId is not provably
            # pre-position either. A close request echoes positionId, so only
            # its orderId decides.
            order_id = _present_proto_field_fail_closed(response, "orderId")
            position_id = _present_proto_field_fail_closed(response, "positionId")
            definitive_refusal = order_id in (None, 0) and (
                self._is_close_position_request(request)
                or position_id in (None, 0)
            )
            message = (
                f"Order rejected ({getattr(response, 'errorCode', '') or ''}): "
                f"{getattr(response, 'description', '') or 'no description'}"
            )
            self._classify_and_raise_refusal(
                message,
                error_code=str(getattr(response, "errorCode", "") or ""),
                definitive=definitive_refusal,
            )
        # Only ProtoOAExecutionEvent carries ``executionType`` in the proto
        # schema, so this attribute sniff is a fallback for an unloaded class,
        # not a general detector: an unclassifiable event must fail closed
        # below, never pass as submitted.
        is_execution_event = (
            isinstance(response, self._ProtoOAExecutionEvent)
            if self._ProtoOAExecutionEvent is not None
            else getattr(response, "executionType", None) is not None
            and getattr(response, "ctidTraderAccountId", None) is not None
        )
        if is_execution_event:
            rejected = self._is_order_rejected_execution(response)
            if rejected is None:
                # The event's type cannot be classified (proto types not fully
                # loaded, or an unreadable executionType): it may be the
                # ORDER_REJECTED refusal this path exists to catch, so it must
                # not fall through and be reported as a submitted order.
                # Removing this raise reopens #428.
                raise RuntimeError(
                    "cTrader order response was an execution event whose "
                    "executionType could not be classified; the outcome is "
                    "unknown and must be reconciled."
                )
            if rejected:
                # Unlike the OrderErrorEvent above, the event typically still
                # carries an `order` object with a broker-assigned orderId, so
                # the orderId cannot decide: definiteness comes from the
                # order's terminal status and the absence of any created
                # state instead.
                error_code = str(getattr(response, "errorCode", "") or "")
                message = (
                    f"Order rejected ({error_code or 'ORDER_REJECTED'}): "
                    f"{getattr(response, 'description', '') or 'no description'}"
                )
                self._classify_and_raise_refusal(
                    message,
                    error_code=error_code,
                    definitive=self._execution_rejection_is_definitive(
                        response, request
                    ),
                )

    def _is_close_position_request(self, request: Any) -> bool:
        """Whether the request is a ClosePositionReq.

        ``_ProtoOAClosePositionReq`` is loaded with the other proto types
        before any submit; if it were missing this returns False and a close
        rejection is evaluated as a new-order one - with the position echo
        that stays unknown, i.e. fail-closed.
        """
        return (
            self._ProtoOAClosePositionReq is not None
            and isinstance(request, self._ProtoOAClosePositionReq)
        )

    def _is_order_rejected_execution(self, event: Any) -> bool | None:
        """Whether the event is an ORDER_REJECTED; ``None`` when unclassifiable.

        ``None`` means the proto type was not fully loaded or the event's
        ``executionType`` cannot be read - the caller must treat the response
        as an unknown outcome rather than letting it pass as submitted.
        """
        execution_type = getattr(self._ProtoOAExecutionType, "ORDER_REJECTED", None)
        if execution_type is None:
            return None
        try:
            return int(getattr(event, "executionType", -1)) == int(execution_type)
        except Exception:  # except-kind: money-decision
            # Any unreadable type is "unclassifiable" (None), never False:
            # False would let the event pass through as a submitted order.
            return None

    def _execution_rejection_is_definitive(self, event: Any, request: Any) -> bool:
        """Whether an ORDER_REJECTED execution event proves nothing was opened.

        Safe means two things: the order can never execute, and no state was
        created. The event's ``order`` must report ``ORDER_STATUS_REJECTED``
        (terminal - a rejected order does not fill later), no ``deal`` may be
        attached (a deal is an execution record, so its presence means an
        execution may exist), and a new-order request must not carry a
        ``position`` (a created position is exactly the state a rejection
        must not have produced). A close request echoes the still-open
        position it failed to close, so position does not decide there -
        the same rule the OrderErrorEvent path applies to ``positionId``.
        Unanswerable field presence fails closed to "unknown".

        Invariant: EVERY presence check in this method goes through
        ``_present_proto_field_fail_closed`` and every attached-message check
        stays an ``is not None`` identity test - presence comes only from
        ``HasField`` (via the fail-closed wrapper); truthiness is not a
        presence test (a protobuf sub-message's truthiness says nothing about
        ``HasField``, and test doubles differ), so it must never decide
        whether a deal/position is attached.
        """
        order = _present_proto_field_fail_closed(event, "order")
        if order is _PRESENT_BUT_UNKNOWN or order is None:
            return False
        status_enum = getattr(self, "_ProtoOAOrderStatus", None)
        rejected_status = (
            getattr(status_enum, "ORDER_STATUS_REJECTED", None)
            if status_enum is not None
            else None
        )
        order_status = getattr(order, "orderStatus", None)
        if order_status is None or rejected_status is None:
            return False
        try:
            if int(order_status) != int(rejected_status):
                return False
        except (TypeError, ValueError):
            return False
        # ``is not None``, never truthiness: presence comes from ``HasField``
        # via the fail-closed wrapper, truthiness is not a presence test, and
        # an attached-but-empty deal is still an execution record whose
        # presence cannot be proven harmless.
        if _present_proto_field_fail_closed(event, "deal") is not None:
            return False
        position = _present_proto_field_fail_closed(event, "position")
        if position is _PRESENT_BUT_UNKNOWN:
            return False
        if self._is_close_position_request(request):
            return True
        # For a new order ANY attached position - even one whose positionId
        # reads as unset; presence comes from ``HasField`` so this must stay
        # an identity check, not truthiness - is state the rejection must
        # not have produced.
        return position is None

    def _handle_spot_event(self, payload: Any) -> None:
        """Feed a spot event to the pending :meth:`get_quote` for its symbol."""
        try:
            symbol_id = int(getattr(payload, "symbolId", 0) or 0)
        except (TypeError, ValueError):
            return
        waiter = self._spot_waiters.get(symbol_id)
        if waiter is None or waiter.future.done():
            return
        # Spot prices are uint64 in 1/100000 of a price unit, like the
        # relative SL/TP distances (123000 means 1.23).
        raw_bid = _present_proto_field(payload, "bid")
        raw_ask = _present_proto_field(payload, "ask")
        raw_timestamp = _present_proto_field(payload, "timestamp")
        try:
            if raw_bid is not None:
                waiter.bid = float(raw_bid) / CTRADER_RELATIVE_PRICE_UNIT_DIVISOR
            if raw_ask is not None:
                waiter.ask = float(raw_ask) / CTRADER_RELATIVE_PRICE_UNIT_DIVISOR
            if raw_timestamp:
                waiter.timestamp = datetime.fromtimestamp(
                    int(raw_timestamp) / 1000, tz=timezone.utc
                )
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            waiter.future.set_exception(
                ValueError(f"cTrader spot event for symbolId {symbol_id} is unreadable: {exc}")
            )
            return
        waiter.received_at = _utc_now()
        if waiter.bid is not None and waiter.ask is not None:
            # The broker's spot time when it sent one (subscribeToSpotTimestamp
            # is requested); otherwise the time this event was received, which
            # is labelled as such. A spot event is pushed live, so its receive
            # time bounds its age; it is never an invented time.
            broker_time = waiter.timestamp
            waiter.future.set_result(
                LiveQuote(
                    bid=waiter.bid,
                    ask=waiter.ask,
                    timestamp=broker_time if broker_time is not None else waiter.received_at,
                    source="ctrader_spot_event",
                    timestamp_source="broker" if broker_time is not None else "receive",
                )
            )

    async def _unsubscribe_spot(self, symbol_id: int) -> None:
        """Best-effort spot unsubscribe; a failure is logged, never raised.

        Also sent after a failed or timed-out subscribe: a subscription the
        broker applied but whose response was lost would otherwise block every
        later subscribe for the symbol (cTrader refuses a second one).
        """
        request = self._ProtoOAUnsubscribeSpotsReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.symbolId.append(int(symbol_id))
        try:
            await self._send_request_with_refresh_retry(request, timeout_seconds=10)
        except Exception as exc:  # except-kind: loop-survival
            logger.warning(
                "cTrader spot unsubscribe failed",
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "symbol_id": int(symbol_id),
                        "error": str(exc),
                    }
                },
            )

    async def get_quote(self, symbol: str) -> LiveQuote:
        """Current bid/ask for the #125 entry-deviation gate.

        Subscribes to the symbol's spot stream, takes the first complete
        bid/ask that arrives after the subscription, then unsubscribes. The
        quote time is the spot event's broker time, else the time the event
        was received (``timestamp_source="receive"``). Raises
        on an unknown symbol, a refused subscription, or no quote within
        ``quote_timeout_seconds``; it never returns an invented price.
        """
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()
        self._raise_if_terminal_auth_failed()
        if self._ProtoOASubscribeSpotsReq is None or self._ProtoOAUnsubscribeSpotsReq is None:
            raise RuntimeError("cTrader spot subscription messages are not loaded")
        symbol_id = int(await self._resolve_symbol_id(symbol))
        lock = self._spot_locks.setdefault(symbol_id, asyncio.Lock())
        async with lock:
            waiter = _SpotWaiter(future=asyncio.get_running_loop().create_future())
            self._spot_waiters[symbol_id] = waiter
            try:
                request = self._ProtoOASubscribeSpotsReq()
                request.ctidTraderAccountId = self.ctid_trader_account_id
                request.symbolId.append(symbol_id)
                request.subscribeToSpotTimestamp = True
                try:
                    await self._send_request_with_refresh_retry(request, timeout_seconds=10)
                except CTraderApiError as exc:
                    self._raise_terminal_auth_from_api_error(exc)
                    raise
                try:
                    return await asyncio.wait_for(
                        waiter.future, timeout=self.quote_timeout_seconds
                    )
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"No cTrader spot quote for {symbol} within "
                        f"{self.quote_timeout_seconds}s"
                    ) from exc
            finally:
                self._spot_waiters.pop(symbol_id, None)
                await self._unsubscribe_spot(symbol_id)

    def supports_market_data_timeframe(self, timeframe: str) -> bool:
        """Whether :meth:`get_market_data` can serve ``timeframe`` (#168).

        Only canonical deployment spellings count: the per-broker trailing
        table and the deployment gates compare the raw timeframe string, so
        an alias like ``60min`` must not pass here and then fail there (or
        vice versa). ``get_market_data`` itself accepts the aliases.
        """
        normalized = str(timeframe or "").strip().lower()
        return normalized in CTRADER_MARKET_DATA_TIMEFRAMES

    async def get_market_data(
        self,
        symbol: str,
        *,
        timeframe: str,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Recent closed trendbars for ``symbol`` on ``timeframe`` (#168).

        Fetches ``ProtoOAGetTrendbarsReq`` history and decodes the
        delta-encoded ``ProtoOATrendbar`` rows into the same OHLCV frame
        shape the Alpaca adapter returns (``timestamp`` tz-aware UTC,
        ascending, ``tail(limit)``). The still-forming bar is excluded: a
        bar counts only once ``open + period <= now``, so no consumer can
        read a close that has not happened yet.

        Raises ``ValueError`` on an unsupported timeframe, an unknown
        symbol, an unusable bar or an empty result — a workflow that cannot
        see its bars must not guess at them.
        """
        self._raise_if_terminal_auth_failed()
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise ValueError("symbol is required for cTrader market data")
        period_name = _trendbar_period_for_timeframe(timeframe)
        if period_name is None:
            raise ValueError(
                f"cTrader cannot serve {timeframe!r} market data "
                f"(supported: {', '.join(sorted(CTRADER_MARKET_DATA_TIMEFRAMES))})"
            )
        period_seconds = _TRENDBAR_PERIOD_SECONDS[period_name]
        if (
            self._ProtoOAGetTrendbarsReq is None
            or self._ProtoOAGetTrendbarsRes is None
            or self._ProtoOATrendbarPeriod is None
        ):
            raise RuntimeError("cTrader trendbar message types are not loaded")
        if not self._connected or not self._authenticated:
            await self.connect()
        self._raise_if_terminal_auth_failed()
        symbol_id = await self._resolve_symbol_id(normalized)
        scale = self._scale_for(normalized)
        bounded_limit = max(1, min(int(limit), 1000))
        now = _utc_now()
        # The window is wider than count*period on purpose: weekends and
        # session gaps mean `count` calendar periods hold fewer bars, and a
        # too-narrow window would silently truncate the history the
        # supertrend warm-up needs.
        window_seconds = period_seconds * bounded_limit * 3
        request = self._ProtoOAGetTrendbarsReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.symbolId = int(symbol_id)
        request.period = getattr(self._ProtoOATrendbarPeriod, period_name)
        request.count = bounded_limit
        request.fromTimestamp = int((now - timedelta(seconds=window_seconds)).timestamp() * 1000)
        request.toTimestamp = int(now.timestamp() * 1000)
        try:
            response = await self._send_request_with_refresh_retry(
                request, timeout_seconds=20
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if not isinstance(response, self._ProtoOAGetTrendbarsRes):
            raise RuntimeError(
                f"Unexpected cTrader trendbars response: {type(response).__name__}"
            )
        rows = decode_trendbars_response(
            response, symbol=normalized, price_digits=scale.price_digits
        )
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise ValueError(
                f"cTrader returned no {timeframe} trendbars for {normalized}"
            )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        # Closed-bar detection: the bar whose period has not fully elapsed is
        # still forming and must not reach a consumer as if it were closed.
        closed = frame[
            frame["timestamp"] + pd.to_timedelta(period_seconds, unit="s") <= now
        ]
        frame = closed.tail(bounded_limit).reset_index(drop=True)
        if frame.empty:
            raise ValueError(
                f"cTrader returned only the still-forming {timeframe} bar for "
                f"{normalized}; no closed bar is available yet"
            )
        return frame

    async def get_pip_value(self, symbol: str) -> LivePipValue:
        """Account-currency value of one qty unit per 1.0 price move (#146).

        One qty unit is ``volume_per_qty`` broker volume, and broker volume is
        in cents of the base asset, so it stands for ``lotSize / 100 / 100``
        base units (EURUSD: 1,000 EUR). A 1.0 price move on one base unit is
        one unit of the quote currency, converted into the account's deposit
        currency with live quotes along the broker's own conversion chain
        (``ProtoOASymbolsForConversionReq``).

        Each hop uses the side that makes the rate HIGHER (the ask when
        multiplying, the bid when dividing): a higher pip value sizes smaller,
        so the spread can only make the loss at the stop smaller than the
        budget, never larger. The quotes are returned so the daemon can refuse
        a stale rate.

        Raises (the daemon refuses the entry) when the symbol has no
        broker-sent ``lotSize`` (the static fallback table is never used for
        sizing), its assets or the account's deposit asset are unknown, the
        chain is missing, too long or does not connect, or a quote is unusable.
        """
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()
        self._raise_if_terminal_auth_failed()
        upper = str(symbol or "").strip().upper()
        scale = self._symbol_props.get(upper)
        if scale is None or scale.source != "broker" or not scale.lot_size_from_broker:
            raise ValueError(
                f"No broker lotSize for cTrader symbol {upper!r}; refusing to size an "
                "entry from the static fallback scale (#146)."
            )
        assets = self._symbol_assets.get(upper)
        if assets is None:
            raise ValueError(
                f"cTrader symbol {upper!r} has no base/quote asset ids; cannot convert "
                "its P&L into the account currency."
            )
        quote_asset_id = assets[1]
        deposit_asset_id = await self._account_deposit_asset_id()
        # Names are labels only: use what is cached and refresh in the
        # background, so a slow asset list never delays entry sizing.
        names = self._asset_names
        self._schedule_asset_name_refresh()
        units_per_qty = scale.volume_per_qty / CTRADER_VOLUME_PER_BASE_UNIT
        if not math.isfinite(units_per_qty) or units_per_qty <= 0:
            raise ValueError(f"cTrader symbol {upper!r} has no positive volume scale")
        rate = 1.0
        conversion_symbols: tuple[str, ...] = ()
        conversion_quotes: tuple[LiveQuote, ...] = ()
        if quote_asset_id != deposit_asset_id:
            rate, conversion_symbols, conversion_quotes = await self._quote_to_deposit_rate(
                quote_asset_id, deposit_asset_id, names=names
            )
        value = units_per_qty * rate
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"cTrader pip value for {upper!r} is not positive: {value!r}")
        return LivePipValue(
            value=value,
            units_per_qty=units_per_qty,
            conversion_rate=rate,
            quote_asset=names.get(quote_asset_id, str(quote_asset_id)),
            account_asset=names.get(deposit_asset_id, str(deposit_asset_id)),
            source="ctrader_symbol_props",
            conversion_symbols=conversion_symbols,
            conversion_quotes=conversion_quotes,
        )

    async def _account_deposit_asset_id(self) -> int:
        """The account's deposit (account-currency) asset id, cached per session."""
        if self._deposit_asset_id is not None:
            return self._deposit_asset_id
        trader = await self._fetch_trader()
        try:
            deposit = int(getattr(trader, "depositAssetId", 0) or 0)
        except (TypeError, ValueError):
            deposit = 0
        if deposit <= 0:
            raise ValueError("cTrader trader has no deposit asset id; account currency unknown")
        self._deposit_asset_id = deposit
        return deposit

    def _schedule_asset_name_refresh(self) -> None:
        """Start one background asset-name fetch when no names are cached."""
        task = self._asset_names_task
        if self._asset_names or (task is not None and not task.done()):
            return
        self._asset_names_task = asyncio.get_running_loop().create_task(self._asset_name_map())

    async def _asset_name_map(self) -> dict[int, str]:
        """Asset id -> name (e.g. 15 -> "USD"), best-effort, for logs only.

        Names never decide anything: the conversion runs on ids. A failed
        fetch only leaves ids in the persisted record.
        """
        if self._asset_names:
            return self._asset_names
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAAssetListReq,
                ProtoOAAssetListRes,
            )

            request = ProtoOAAssetListReq()
            request.ctidTraderAccountId = self.ctid_trader_account_id
            response = await self._send_request_with_refresh_retry(request, timeout_seconds=15)
            if isinstance(response, ProtoOAAssetListRes):
                self._asset_names = {
                    int(asset.assetId): str(asset.name) for asset in response.asset
                }
        except Exception as exc:  # except-kind: loop-survival
            logger.warning(
                "cTrader asset list unavailable; pip value record keeps asset ids",
                extra={"data": {"account_id": self.ctid_trader_account_id, "error": str(exc)}},
            )
        return self._asset_names

    async def _quote_to_deposit_rate(
        self, quote_asset_id: int, deposit_asset_id: int, *, names: Mapping[int, str]
    ) -> tuple[float, tuple[str, ...], tuple[LiveQuote, ...]]:
        """Deposit-currency value of one unit of ``quote_asset_id`` (#146).

        Walks the broker's conversion chain hop by hop: for a symbol B/Q at
        price p, 1 B = p Q. Holding B, the next asset is Q (multiply by the
        ask); holding Q, it is B (divide by the bid). Both sides are the
        conservative ones (see :meth:`get_pip_value`).
        """
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASymbolsForConversionReq,
            ProtoOASymbolsForConversionRes,
        )

        label = (
            f"{names.get(quote_asset_id, quote_asset_id)}->"
            f"{names.get(deposit_asset_id, deposit_asset_id)}"
        )
        request = ProtoOASymbolsForConversionReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.firstAssetId = int(quote_asset_id)
        request.lastAssetId = int(deposit_asset_id)
        try:
            response = await self._send_request_with_refresh_retry(request, timeout_seconds=15)
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if not isinstance(response, ProtoOASymbolsForConversionRes):
            raise RuntimeError(
                f"Unexpected cTrader conversion response: {type(response).__name__}"
            )
        chain = list(response.symbol)
        if not chain:
            raise ValueError(f"cTrader has no conversion chain for {label}")
        if len(chain) > MAX_CONVERSION_HOPS:
            raise ValueError(
                f"cTrader conversion chain for {label} has {len(chain)} hops "
                f"(limit {MAX_CONVERSION_HOPS})"
            )
        current = int(quote_asset_id)
        rate = 1.0
        symbols: list[str] = []
        quotes: list[LiveQuote] = []
        for light in chain:
            name = str(getattr(light, "symbolName", "") or "").upper()
            assets = _light_symbol_assets(light)
            if not name or assets is None:
                raise ValueError(f"cTrader conversion chain for {label} has an incomplete symbol")
            base_id, quote_id = assets
            if current not in (base_id, quote_id):
                raise ValueError(
                    f"cTrader conversion chain for {label} does not connect at {name}"
                )
            quote = await self.get_quote(name)
            bid, ask = _conversion_quote_prices(quote, symbol=name)
            if current == base_id:
                rate *= ask
                current = quote_id
            else:
                rate /= bid
                current = base_id
            symbols.append(name)
            quotes.append(quote)
        if current != int(deposit_asset_id):
            raise ValueError(
                f"cTrader conversion chain for {label} does not end in the account currency"
            )
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"cTrader conversion rate for {label} is not positive: {rate!r}")
        return rate, tuple(symbols), tuple(quotes)

    async def get_max_leverage(self, symbol: str) -> float | None:
        """The broker's maximum leverage for this account and ``symbol`` (#240).

        Two broker-granted ceilings bound the answer and the tighter one wins:

        * the symbol's dynamic-leverage tiers — ``ProtoOASymbol.leverageId``,
          captured with the symbol props, resolves through
          ``ProtoOAGetDynamicLeverageByIDReq`` into volume tiers sorted
          ascending by volume. ``ProtoOADynamicLeverageTier.leverage`` is in
          the cents convention (1:200 is ``20000``): Spotware's own Open API
          example divides the field by 100 before display, so the plain ratio
          is ``value / 100``. Leverage shrinks as position volume grows, so
          the maximum sits in the first tier — an upper bound: a larger order
          is margined at a lower tier, which the broker still enforces;
        * the account's own leverage — ``ProtoOATrader.leverageInCents`` is
          the leverage the account actually trades at. Spotware's example
          computes the effective leverage as
          ``min(trader.leverageInCents, tier.leverage) / 100``, so an account
          set to 1:50 on a symbol offering 1:500 is capped at 50 — the tier
          alone would overstate what the broker will margin.
          ``ProtoOATrader.maxLeverage`` is deliberately *not* read: it is a
          plain uint32 named without an "InCents" suffix (Spotware uses it to
          validate leverage *changes*, not margining), the broker already
          validates ``leverageInCents`` against it so it can never be the
          tighter bound, and reading it in the wrong unit would silently
          shrink every order.

        The account leg degrades on its own (no readable trader record or an
        unset/nonpositive field contributes nothing), while a failed tier
        request fails the whole lookup closed — ``None``, never a partial
        answer — so callers treat ``None`` as 1x.
        """
        try:
            self._raise_if_terminal_auth_failed()
            if not self._connected or not self._authenticated:
                await self.connect()
            self._raise_if_terminal_auth_failed()

            account_cents = await self._account_leverage_cents()

            tier_cents: float | None = None
            if (
                self._ProtoOAGetDynamicLeverageByIDReq is not None
                and self._ProtoOAGetDynamicLeverageByIDRes is not None
            ):
                leverage_id = self._symbol_leverage_ids.get(
                    str(symbol or "").strip().upper()
                )
                if leverage_id is not None:
                    request = self._ProtoOAGetDynamicLeverageByIDReq()
                    request.ctidTraderAccountId = self.ctid_trader_account_id
                    request.leverageId = int(leverage_id)
                    response = await self._send_request_with_refresh_retry(
                        request, timeout_seconds=10
                    )
                    if isinstance(
                        response, self._ProtoOAGetDynamicLeverageByIDRes
                    ):
                        leverage_msg = getattr(response, "leverage", None)
                        tiers = (
                            getattr(leverage_msg, "tiers", None)
                            if leverage_msg is not None
                            else None
                        )
                        values = [
                            value
                            for tier in tiers or []
                            if (value := _optional_float(getattr(tier, "leverage", None)))
                            is not None
                            and math.isfinite(value)
                            and value > 0
                        ]
                        if values:
                            tier_cents = max(values)

            candidates = [cents for cents in (tier_cents, account_cents) if cents is not None]
            return min(candidates) / 100.0 if candidates else None
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "cTrader max leverage lookup failed; treating it as unknown (1x)",
                extra={"data": {"symbol": symbol, "error": repr(exc)}},
            )
            return None

    async def _account_leverage_cents(self) -> float | None:
        """The account's own leverage in cents, or ``None``.

        Reads ``ProtoOATrader.leverageInCents`` — the leverage the account
        actually trades at — which is the account-side bound the symbol
        tiers are clamped against. ``maxLeverage`` is not read: its unit is
        unverified and it is only the broker's ceiling for *changing*
        leverage, which ``leverageInCents`` is already validated against.
        An unreadable trader record or an unset/nonpositive field degrades
        to ``None`` — the tier leg may still answer on its own.
        """
        try:
            trader = await self._fetch_trader()
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "cTrader trader leverage lookup failed; using the symbol tiers only",
                extra={"data": {"error": repr(exc)}},
            )
            return None
        value = _optional_float(getattr(trader, "leverageInCents", None))
        if value is None or not math.isfinite(value) or value <= 0:
            return None
        return value

    async def _fetch_trader(self) -> Any:
        """The account's ``ProtoOATrader`` (balance, money digits, deposit asset)."""
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
        return trader_res.trader

    async def account_type(self) -> str | None:
        """The account's ``ProtoOAAccountType`` as ``hedged``/``netted``/``spread_betting``.

        ``None`` when the trader record cannot be read or carries a type this
        build does not know - callers must treat "not proven hedged" as the
        unsafe case (#382: ``internal_max_open_per_symbol > 1`` is refused on
        cTrader accounts that cannot prove they hedge positions). Only a
        successful read is cached permanently; an unreadable read is
        negatively cached for ``_ACCOUNT_TYPE_RETRY_SECONDS`` so a broker
        outage does not cost a request timeout on every runner pass, and
        never pins "unknown" past the window.
        """
        if self._account_type is not None:
            return self._account_type
        now = time.monotonic()
        if (
            self._account_type_unreadable_at is not None
            and now - self._account_type_unreadable_at < _ACCOUNT_TYPE_RETRY_SECONDS
        ):
            return None
        try:
            trader = await self._fetch_trader()
        except Exception:  # except-kind: money-decision
            self._account_type_unreadable_at = now
            return None
        # ``accountType`` is a proto2 optional whose declared default is
        # HEDGED: a trader record that does not carry the field reads as
        # ``0`` -> "hedged", which would fail OPEN on exactly the guard this
        # read exists for. ``HasField`` is the only proof the broker stated
        # a type; stubs without it (SimpleNamespace) keep the getattr path.
        if hasattr(trader, "HasField") and not trader.HasField("accountType"):
            self._account_type_unreadable_at = time.monotonic()
            return None
        try:
            raw_type = int(getattr(trader, "accountType", -1))
        except (TypeError, ValueError):
            self._account_type_unreadable_at = time.monotonic()
            return None
        name = _CTRADER_ACCOUNT_TYPES.get(raw_type)
        if name is None:
            self._account_type_unreadable_at = time.monotonic()
            return None
        self._account_type = name
        return name

    async def _fetch_position_unrealized_pnl(self, fallback_money_digits: int) -> float:
        """Summed net unrealized P&L of all open positions, in account currency.

        ``ProtoOAGetPositionUnrealizedPnLRes`` reports one entry per open
        position with ``grossUnrealizedPnL``/``netUnrealizedPnL`` in money
        digits; the net figure (after accrued swap/commission) is what the
        account's equity actually carries. The response carries its own
        ``moneyDigits``; the trader's digits are the fallback when it is unset.
        Any failure raises - the caller treats unreadable equity as unreadable,
        never as "no unrealized loss".
        """
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetPositionUnrealizedPnLReq,
            ProtoOAGetPositionUnrealizedPnLRes,
        )

        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()

        self._raise_if_terminal_auth_failed()
        request = ProtoOAGetPositionUnrealizedPnLReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        try:
            response = await self._send_request_with_refresh_retry(
                request, timeout_seconds=15
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise
        if not isinstance(response, ProtoOAGetPositionUnrealizedPnLRes):
            raise RuntimeError(
                f"Unexpected unrealized PnL response: {type(response).__name__}"
            )
        digits = int(getattr(response, "moneyDigits", 0) or 0)
        if digits <= 0:
            digits = int(fallback_money_digits or 0)
        scale = money_scale(digits)
        total = 0.0
        for position_pnl in response.positionUnrealizedPnL:
            # Fail closed per entry (#302): an unreadable or non-finite
            # netUnrealizedPnL read as 0.0 would understate a losing position
            # and overstate equity - the unsafe direction for both the loss
            # breaker and the pre-submit caps' free-margin ceiling.
            # ``parse_money`` raises ``MoneyParseError`` on a missing or
            # garbage field, so the whole equity read fails closed.
            total += parse_money(getattr(position_pnl, "netUnrealizedPnL", None)) / scale
        return total

    async def get_balance(self) -> tuple[float, float]:
        trader = await self._fetch_trader()
        money_digits = money_digits_from_trader(trader)
        scale = money_scale(money_digits)
        balance = parse_money(getattr(trader, "balance", 0)) / scale
        equity = balance
        if hasattr(trader, "equity"):
            raw_equity = parse_optional(getattr(trader, "equity", None))
            if raw_equity is not None:
                equity = raw_equity / scale
        return balance, equity

    async def get_used_margin(self) -> float | None:
        """Margin held against open positions, summed in account currency (#359).

        Sums the broker-reported ``ProtoOAPosition.usedMargin`` of every OPEN
        position — cTrader has already applied each position's own leverage
        (account leverage clamped by the symbol's volume tier), so no leverage
        lookup is needed here. ``None`` when the reconcile or any open
        position's margin figure is unreadable: a partial sum would understate
        used margin and overstate the free margin the pre-submit caps judge
        the order against.
        """
        try:
            self._raise_if_terminal_auth_failed()
            if not self._connected or not self._authenticated:
                await self.connect()
            reconcile_res = await self._reconcile_state()
            total = 0.0
            for position in getattr(reconcile_res, "position", []) or []:
                status = int(getattr(position, "positionStatus", 0) or 0)
                if status != self._ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                    continue
                used = _optional_float(getattr(position, "usedMargin", None))
                digits = getattr(position, "moneyDigits", None)
                if used is None or used < 0 or digits is None:
                    return None
                try:
                    digits_int = int(digits)
                except (TypeError, ValueError, OverflowError):
                    return None
                if digits_int < 0:
                    return None
                total += used / money_scale(digits_int)
            return total
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "cTrader used-margin lookup failed; treating it as unknown",
                extra={"data": {"error": repr(exc)}},
            )
            return None

    async def get_equity(self) -> float:
        """Account equity including unrealized P&L, for the loss breaker (#301).

        Deliberately a separate path from :meth:`get_balance`: the #267
        maintainer decision keeps position sizing on realized balance, so
        ``get_balance`` must not start carrying unrealized P&L. The loss
        breaker (#301) and the pre-submit caps' free-margin ceiling (#302,
        #359) read this. ``ProtoOATrader`` has no equity field, so equity
        is balance plus the summed net unrealized P&L of open positions -
        reporting balance here would hide an open losing position until it
        closes. Any failure raises: unreadable equity is never "no loss".
        """
        trader = await self._fetch_trader()
        money_digits = money_digits_from_trader(trader)
        scale = money_scale(money_digits)
        balance = parse_money(getattr(trader, "balance", None)) / scale
        if hasattr(trader, "equity"):
            # A trader payload that carries its own equity field is trusted to
            # include unrealized P&L already (ProtoOATrader does not). An
            # absent field falls back to balance plus unrealized P&L; a
            # present-but-unreadable one raises via parse_optional.
            raw_equity = parse_optional(getattr(trader, "equity", None))
            if raw_equity is not None:
                return raw_equity / scale
        return balance + await self._fetch_position_unrealized_pnl(money_digits)

    @property
    def supports_cancel_orders(self) -> bool:
        return True

    @property
    def supports_flatten(self) -> bool:
        return True

    async def _reconcile_state(self) -> Any:
        """The account's current orders and positions, from one call.

        Reconcile, not OrderList. `ProtoOAOrderListReq` is a historical query bounded by
        `fromTimestamp`/`toTimestamp`; 0/0 does not mean "now", so it can answer with nothing -
        and a cancel that finds nothing reports success while leaving the order resting - or
        with unrelated history. Reconcile takes no time bounds and is by definition current
        state, which is the same call `_fetch_open_positions` already makes for positions.

        Shared by the blanket cancel, the orphan sweep and the flatten so all three see one
        consistent snapshot: a sweep that read positions and orders from two different
        snapshots could cancel a protection for a position that had just been closed and
        re-opened.
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

    async def _fetch_open_positions(self) -> list[Any]:
        """Every open position, including ones whose trade data is unreadable.

        The unreadable ones are deliberately *not* filtered out here: `get_positions` skips them
        because it cannot describe them, but the flatten has to report them, or a position the
        adapter could not read would count as one that does not exist.
        """
        reconcile_res = await self._reconcile_state()
        open_positions: list[Any] = []
        for position in getattr(reconcile_res, "position", []) or []:
            status = int(getattr(position, "positionStatus", 0) or 0)
            if status != self._ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            open_positions.append(position)
        return open_positions

    def _unrecognised_status_wire_value(self, order: Any) -> int | None:
        """The status number the broker sent, when this client has no name for it.

        An enum value this build does not know is not lost: protobuf keeps it in the message's
        unknown fields, under `orderStatus`'s own field number, with the original value intact.
        A status that was never sent leaves no unknown field at all. So "the broker sent nothing"
        and "the broker sent a status this client cannot name" are distinguishable, and this
        asks the question rather than treating the second as the first - which would read a
        future terminal status as ACCEPTED and cancel it.

        Returns None for anything that cannot carry the fingerprint - a test double, or a build
        without explicit presence - so the ordinary path is unaffected.
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
        try:
            unknown = list(unknown_fields())
        except TypeError:
            return None
        for field in unknown:
            # Wire type 0 is varint, which is how an enum arrives on the wire.
            if field.field_number in wanted and field.wire_type == 0:
                return int(field.data)
        return None

    def _is_working_order(self, order: Any) -> bool:
        """Whether an order is still ACCEPTED, i.e. a cancel would be legal.

        A filled or already-cancelled order must not be sent for cancellation: cTrader rejects
        it, and the rejection is a per-order failure the sweep has to record rather than an
        abort. Only ACCEPTED orders are working; anything else is already terminal.

        Three "cannot tell" paths, all resolved in the direction that leaves nothing resting,
        because that is the unsafe direction for a sweep that exists to disarm the account:
        no status enum, no status field, and a status this client has no name for. The last one
        arrives looking exactly like ACCEPTED; it is detected and logged, while the decision
        stays as it was - which failure is cheaper here is a trading judgement.
        """
        unrecognised = self._unrecognised_status_wire_value(order)
        if unrecognised is not None:
            logger.warning(
                "cTrader sent order status %s, which this client has no name for. It reads as "
                "ACCEPTED, so this order is being cancelled on a status nobody here could "
                "interpret; if that status is terminal the broker may refuse the cancel.",
                unrecognised,
            )
        status_enum = getattr(self, "_ProtoOAOrderStatus", None)
        if status_enum is None:
            logger.warning(
                "cTrader client exposes no order-status enum, so every order is treated as "
                "working and a cancel sweep will send a cancel for each",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            return True
        raw_status = getattr(order, "orderStatus", None)
        if raw_status is None:
            raw_status = getattr(order, "status", None)
        if raw_status is None:
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
            return int(raw_status) == int(status_enum.ORDER_STATUS_ACCEPTED)
        except (TypeError, ValueError):
            # The one path that resolves *against* cancelling, and the only silent one. It is
            # logged because it disagrees with the other three: an order whose status cannot be
            # parsed would otherwise be left resting with no trace at all.
            logger.warning(
                "cTrader order status %r is not an integer, so it is treated as NOT working and "
                "will not be cancelled by a sweep; this resolves in the opposite direction from "
                "the other unrecognised-status paths, which assume working",
                raw_status,
                extra={
                    "data": {
                        "account_id": self.ctid_trader_account_id,
                        "order_id": getattr(order, "orderId", None),
                    }
                },
            )
            return False

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        """`int(value)` when it is a positive integer, else None.

        The proto fields these feed (`orderId`, `positionId`, `volume`) are int64: assigning the
        string form raises TypeError on the wire, and 0 is rejected by the broker. Both are
        "unidentifiable" for the sweep's purposes, not "nothing to do".
        """
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed > 0 else None

    def _raise_if_order_error_event(self, response: Any) -> None:
        """Turn a trading refusal into a `CTraderApiError`.

        `_send_request` only raises for `ProtoOAErrorRes`. cTrader reports trading refusals
        (``MARKET_CLOSED``, ``POSITION_NOT_FOUND``, ``TRADING_BAD_VOLUME``, ...) as a
        `ProtoOAOrderErrorEvent`, which arrives as an ordinary *successful* response. A control
        sweep that only watches for `CTraderApiError` would count a refused close or cancel as
        completed, and the daemon would then stamp the deployment as enforced while the position
        is still open. The submit path performs the same check in `_submit_new_order_request`.
        """
        event_type = self._ProtoOAOrderErrorEvent
        if event_type is not None and isinstance(response, event_type):
            raise CTraderApiError(
                error_code=str(getattr(response, "errorCode", "")),
                description=str(getattr(response, "description", "")) or "no description",
            )

    async def _cancel_order_id(self, order_id: int) -> None:
        """Send one cancel for a validated positive order id.

        Raises `CTraderApiError` for both an API error response and a trading refusal.
        """
        cancel_req = self._ProtoOACancelOrderReq()
        cancel_req.ctidTraderAccountId = self.ctid_trader_account_id
        cancel_req.orderId = order_id
        try:
            response = await self._send_request_with_refresh_retry(cancel_req, timeout_seconds=10)
            self._raise_if_order_error_event(response)
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            raise

    async def working_order_ids(self) -> set[str]:
        """Ids of every working order: order ids and client order ids.

        Used before sizing an exit, to tell whether an earlier exit is still
        working (#121). "Working" is :meth:`_is_working_order`, which reads a
        status it cannot interpret as working - the direction that defers an
        exit rather than sizing it off a ledger that may lack fills.
        """
        reconcile_res = await self._reconcile_state()
        ids: set[str] = set()
        for order in getattr(reconcile_res, "order", []) or []:
            if not self._is_working_order(order):
                continue
            order_id = self._positive_int(getattr(order, "orderId", 0))
            if order_id is not None:
                ids.add(str(order_id))
            client_order_id = str(getattr(order, "clientOrderId", "") or "").strip()
            if client_order_id:
                ids.add(client_order_id)
        return ids

    async def list_reconciliation_orders(self) -> list[BrokerOrderView]:
        """Working orders with client ids and position-protection marks (#303)."""
        reconcile_res = await self._reconcile_state()
        trade_side_enum = getattr(self, "_ProtoOATradeSide", None)
        if trade_side_enum is None:
            raise ValueError("cTrader trade-side enum is unavailable")
        try:
            buy_side = int(trade_side_enum.BUY)
            sell_side = int(trade_side_enum.SELL)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("cTrader trade-side enum is unreadable") from exc

        def _trade_int(value: Any, field: str, order_key: str) -> int:
            if isinstance(value, bool):
                raise ValueError(
                    f"cTrader working order {order_key} has unreadable tradeData.{field}"
                )
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"cTrader working order {order_key} has unreadable tradeData.{field}"
                ) from exc
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(
                    f"cTrader working order {order_key} has unreadable tradeData.{field}"
                )
            return number

        views: list[BrokerOrderView] = []
        for order in getattr(reconcile_res, "order", []) or []:
            if not self._is_working_order(order):
                continue
            try:
                order_type = int(getattr(order, "orderType", 0) or 0)
            except (TypeError, ValueError):
                order_type = 0
            order_id = self._positive_int(getattr(order, "orderId", 0))
            client_order_id = str(getattr(order, "clientOrderId", "") or "").strip()
            key = str(order_id) if order_id is not None else client_order_id
            if not key:
                continue

            # ProtoOAOrder carries symbol, volume and side under tradeData,
            # not at the message root. A missing or malformed tradeData makes
            # the whole broker read unreadable: emitting a blank/zero view
            # would let reconciliation treat an unpriced order as accounted
            # for instead of failing closed.
            trade_data = getattr(order, "tradeData", None)
            if trade_data is None:
                raise ValueError(f"cTrader working order {key} has no tradeData")
            symbol_id = _trade_int(
                getattr(trade_data, "symbolId", 0), "symbolId", key
            )
            if symbol_id <= 0:
                raise ValueError(
                    f"cTrader working order {key} has no tradeData.symbolId"
                )
            symbol = self._symbol_name_for_id(symbol_id)

            volume_raw = getattr(trade_data, "volume", 0)
            if isinstance(volume_raw, bool):
                raise ValueError(
                    f"cTrader working order {key} has unreadable tradeData.volume"
                )
            try:
                volume = float(volume_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"cTrader working order {key} has unreadable tradeData.volume"
                ) from exc
            if not math.isfinite(volume) or volume <= 0:
                raise ValueError(
                    f"cTrader working order {key} has non-positive tradeData.volume"
                )

            trade_side = _trade_int(
                getattr(trade_data, "tradeSide", 0), "tradeSide", key
            )
            if trade_side == buy_side:
                side = "buy"
            elif trade_side == sell_side:
                side = "sell"
            else:
                raise ValueError(
                    f"cTrader working order {key} has unknown tradeData.tradeSide"
                )

            scale = self._scale_for(symbol)
            qty = _broker_volume_to_qty(volume, scale.volume_per_qty)
            if not math.isfinite(qty) or qty <= 0:
                raise ValueError(
                    f"cTrader working order {key} has unreadable tradeData volume"
                )

            protection = order_type == int(self._ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT)
            views.append(
                BrokerOrderView(
                    order_id=key,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type=str(order_type),
                    protection=protection,
                )
            )
        return views

    async def get_order_state(self, order_id: str, *, client_order_id: str = "") -> str | None:
        """Working/terminal state of one order for reconciliation (#303).

        ``ProtoOAOrderDetailsReq`` answers for a specific order id, including
        orders no longer working. ``None`` - a transport error, an unknown
        order, a missing status or a status this client cannot name - always
        fails closed: the reconciler keeps its finding.
        """
        order_id_int = self._coerce_broker_order_id(order_id)
        if order_id_int is None:
            return None
        self._raise_if_terminal_auth_failed()
        if not self._connected or not self._authenticated:
            await self.connect()
        self._raise_if_terminal_auth_failed()
        if (
            self._ProtoOAOrderDetailsReq is None
            or self._ProtoOAOrderDetailsRes is None
            or self._ProtoOAOrderStatus is None
        ):
            return None
        details_req = self._ProtoOAOrderDetailsReq()
        details_req.ctidTraderAccountId = self.ctid_trader_account_id
        details_req.orderId = order_id_int
        try:
            details_res = await self._send_request_with_refresh_retry(
                details_req, timeout_seconds=15
            )
        except CTraderApiError:
            return None
        if not isinstance(details_res, self._ProtoOAOrderDetailsRes):
            return None
        order = getattr(details_res, "order", None)
        if order is None:
            return None
        # ``orderStatus`` defaults to ACCEPTED on an unset order, so a broker
        # response without a real order id must read as unknown, not working.
        if not getattr(order, "orderId", 0):
            return None
        raw_status = getattr(order, "orderStatus", None)
        if raw_status is None:
            raw_status = getattr(order, "status", None)
        if raw_status is None:
            return None
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            return None
        status_enum = self._ProtoOAOrderStatus
        if status == int(status_enum.ORDER_STATUS_ACCEPTED):
            return ORDER_STATE_WORKING
        if status == int(status_enum.ORDER_STATUS_FILLED):
            return ORDER_STATE_FILLED
        terminal = {
            int(getattr(status_enum, name))
            for name in (
                "ORDER_STATUS_REJECTED",
                "ORDER_STATUS_EXPIRED",
                "ORDER_STATUS_CANCELLED",
            )
            if getattr(status_enum, name, None) is not None
        }
        if status in terminal:
            return ORDER_STATE_TERMINAL
        return None

    async def get_order_details(self, order_id: int, *, timeout_seconds: int = 20) -> Any:
        """Raw ``ProtoOAOrderDetailsRes`` for one order on this account.

        Public read seam for operator tooling (``scripts/verify_ctrader_order.py``);
        the caller must already be connected and authenticated. Unlike
        :meth:`get_order_state` this does not fail closed - transport and auth
        errors propagate so the verifier can report them.
        """
        if self._ProtoOAOrderDetailsReq is None:
            raise RuntimeError(
                "cTrader order-details protobuf is unavailable; "
                "the ctrader-open-api package failed to load."
            )
        details_req = self._ProtoOAOrderDetailsReq()
        details_req.ctidTraderAccountId = self.ctid_trader_account_id
        details_req.orderId = int(order_id)
        return await self._send_request_with_refresh_retry(
            details_req, timeout_seconds=timeout_seconds
        )

    def position_qty_tolerance(self, symbol: str) -> float:
        """Half of one broker volume unit, in qty units, for ``symbol``.

        Positions and fills are whole broker volume units converted by the
        symbol's ``volume_per_qty``, so anything closer than half a unit is the
        same position. Raises like :meth:`_scale_for` for an unknown symbol; the
        caller then falls back to the strict default.
        """
        return 0.5 / float(self._scale_for(symbol).volume_per_qty)

    async def cancel_open_orders(self) -> ControlOutcome:
        """Cancel every working *detached* order for this cTrader account.

        `STOP_LOSS_TAKE_PROFIT` is deliberately not cancellable here. That type is not a resting
        entry: it is the stop and target attached to an open position, and cancelling it leaves
        that position with no stop and no target. A blanket cancel runs before the flatten, so
        the naked-position window is not a formality - if the flatten then fails, the position
        survives with every risk control removed. Protections that outlive their position are
        removed by :meth:`cancel_orphaned_protections` instead.

        One refusal does not strand the rest: the remaining orders still need cancelling, and a
        rejection on one says nothing about the next. Every refusal and every unaddressable
        order is reported in the outcome, because a caller cannot tell a complete sweep from a
        partial one by the count alone.
        """
        reconcile_res = await self._reconcile_state()

        # Market and market-range orders fill immediately and cannot be cancelled.
        cancellable = {
            int(self._ProtoOAOrderType.LIMIT),
            int(self._ProtoOAOrderType.STOP),
            int(self._ProtoOAOrderType.STOP_LIMIT),
        }
        cancelled = 0
        unidentified: list[str] = []
        failed: list[str] = []
        # `ProtoOAReconcileRes` names the repeated field `order`, not `orders`.
        for order in getattr(reconcile_res, "order", []) or []:
            try:
                order_type = int(getattr(order, "orderType", 0) or 0)
            except (TypeError, ValueError):
                order_type = 0
            if order_type not in cancellable:
                continue
            if not self._is_working_order(order):
                continue
            order_id = self._positive_int(getattr(order, "orderId", 0))
            if order_id is None:
                unidentified.append(f"order:{getattr(order, 'orderId', None)!r}")
                continue
            try:
                await self._cancel_order_id(order_id)
            except CTraderApiError as exc:
                # `CTraderAuthTerminalError` is a RuntimeError, not a CTraderApiError, so an
                # auth-terminal failure still propagates and ends the sweep - correct, because
                # the connection is no longer usable and the remaining cancels would fail too.
                self._raise_terminal_auth_from_api_error(exc)
                failed.append(str(order_id))
                logger.warning(
                    "cTrader refused to cancel order %s: %s. Continuing the sweep - the "
                    "remaining orders still need cancelling.",
                    order_id,
                    exc,
                )
                continue
            cancelled += 1
        return ControlOutcome(
            completed=cancelled,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
        )

    async def cancel_orders(
        self, order_refs: Collection[str], *, symbol: str
    ) -> ControlOutcome:
        """Cancel the working orders ``order_refs`` names on ``symbol`` (#382).

        Matches a working order on its broker ``orderId`` or its
        ``clientOrderId`` (the persisted signal id) and only when the order's
        symbol is ``symbol``. The same type gate as
        :meth:`cancel_open_orders` applies, so attached position protections
        (``STOP_LOSS_TAKE_PROFIT``) can never be caught by a ref that
        happens to collide: they are not in the cancellable set at all.
        """
        wanted_symbol = str(symbol or "").strip().upper()
        refs = {str(ref).strip() for ref in order_refs if str(ref or "").strip()}
        if not refs or not wanted_symbol:
            return ControlOutcome()
        reconcile_res = await self._reconcile_state()
        cancellable = {
            int(self._ProtoOAOrderType.LIMIT),
            int(self._ProtoOAOrderType.STOP),
            int(self._ProtoOAOrderType.STOP_LIMIT),
        }
        cancelled = 0
        unidentified: list[str] = []
        failed: list[str] = []
        for order in getattr(reconcile_res, "order", []) or []:
            try:
                order_type = int(getattr(order, "orderType", 0) or 0)
            except (TypeError, ValueError):
                order_type = 0
            if order_type not in cancellable:
                continue
            if not self._is_working_order(order):
                continue
            order_id = self._positive_int(getattr(order, "orderId", 0))
            client_order_id = str(getattr(order, "clientOrderId", "") or "").strip()
            matched = (
                (order_id is not None and str(order_id) in refs)
                or (client_order_id and client_order_id in refs)
            )
            if not matched:
                continue
            trade_data = getattr(order, "tradeData", None)
            symbol_id = (
                self._positive_int(getattr(trade_data, "symbolId", 0))
                if trade_data is not None
                else None
            )
            try:
                order_symbol = (
                    self._symbol_name_for_id(symbol_id).strip().upper()
                    if symbol_id is not None
                    else ""
                )
            except ValueError:
                order_symbol = ""
            if not order_symbol:
                # A matched ref whose symbol cannot be resolved can be
                # neither proven ours-for-this-symbol nor safely cancelled:
                # report it rather than skip it silently.
                unidentified.append(
                    f"order:{order_id if order_id is not None else client_order_id}"
                )
                continue
            if order_symbol != wanted_symbol:
                continue
            if order_id is None:
                unidentified.append(
                    f"order:{client_order_id or getattr(order, 'orderId', None)!r}"
                )
                continue
            try:
                await self._cancel_order_id(order_id)
            except CTraderApiError as exc:
                self._raise_terminal_auth_from_api_error(exc)
                failed.append(str(order_id))
                logger.warning(
                    "cTrader refused to cancel named order %s: %s.",
                    order_id,
                    exc,
                )
                continue
            cancelled += 1
        return ControlOutcome(
            completed=cancelled,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
        )

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        """Cancel attached SL/TP orders whose position no longer exists.

        `cancel_open_orders` never disarms a live position, so closing a position leaves its
        protection behind: it can no longer fill, it holds an order slot, and it makes a
        flattened account look like something is still resting. This removes exactly those and
        nothing else:

        - the order type must be STOP_LOSS_TAKE_PROFIT (no other type is attached);
        - its `positionId` must be a positive integer *absent* from the open-position set of
          the same snapshot. An unset or unreadable `positionId` is left alone: "I cannot tell
          which position this belongs to" must never resolve to "cancel the stop on a position
          that is still open";
        - it must be ACCEPTED, exactly like the blanket cancel.
        """
        reconcile_res = await self._reconcile_state()
        open_position_ids: set[int] = set()
        for position in getattr(reconcile_res, "position", []) or []:
            if int(getattr(position, "positionStatus", 0) or 0) != (
                self._ProtoOAPositionStatus.POSITION_STATUS_OPEN
            ):
                continue
            position_id = self._positive_int(getattr(position, "positionId", 0))
            if position_id is not None:
                open_position_ids.add(position_id)

        cancelled = 0
        failed: list[str] = []
        for order in getattr(reconcile_res, "order", []) or []:
            try:
                order_type = int(getattr(order, "orderType", 0) or 0)
            except (TypeError, ValueError):
                continue
            if order_type != int(self._ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT):
                continue
            position_id = self._positive_int(getattr(order, "positionId", 0))
            if position_id is None or position_id in open_position_ids:
                continue
            if not self._is_working_order(order):
                continue
            order_id = self._positive_int(getattr(order, "orderId", 0))
            if order_id is None:
                continue
            try:
                await self._cancel_order_id(order_id)
            except CTraderApiError as exc:
                self._raise_terminal_auth_from_api_error(exc)
                failed.append(str(order_id))
                continue
            cancelled += 1
        return ControlOutcome(completed=cancelled, failed=tuple(failed))

    async def close_all_positions(self) -> ControlOutcome:
        """Flatten every open position for this cTrader account, at market.

        A position whose id or volume cannot be read is reported as unidentifiable rather than
        skipped: it is still open, and a caller that only sees a count would read the sweep as
        successful while exposure remains. The same holds for a broker refusal - the other
        positions are still attempted, and the refusal is named in the outcome.
        """
        positions = await self._fetch_open_positions()
        closed = 0
        unidentified: list[str] = []
        failed: list[str] = []
        for position in positions:
            # `positionId` and `volume` are protobuf int64 fields: the string form cannot be
            # assigned, a float raises TypeError, and an unset volume serializes as 0, which
            # cTrader rejects instead of closing.
            position_id = self._positive_int(getattr(position, "positionId", 0))
            if position_id is None:
                unidentified.append(f"position:{getattr(position, 'positionId', None)!r}")
                continue
            trade_data = getattr(position, "tradeData", None)
            try:
                raw_volume = (
                    parse_optional(getattr(trade_data, "volume", None))
                    if trade_data is not None
                    else None
                )
            except ValueError:
                # An unreadable volume is unidentified, not a zero: reporting it
                # keeps the flatten honest instead of closing nothing silently.
                raw_volume = None
            volume = self._positive_int(raw_volume)
            if volume is None:
                unidentified.append(f"position:{position_id}:volume")
                continue
            close_req = self._ProtoOAClosePositionReq()
            close_req.ctidTraderAccountId = self.ctid_trader_account_id
            close_req.positionId = position_id
            close_req.volume = volume
            try:
                response = await self._send_request_with_refresh_retry(
                    close_req, timeout_seconds=15
                )
                self._raise_if_order_error_event(response)
            except CTraderApiError as exc:
                self._raise_terminal_auth_from_api_error(exc)
                failed.append(str(position_id))
                logger.warning(
                    "cTrader refused to close position %s: %s. Continuing the flatten - the "
                    "remaining positions are still open.",
                    position_id,
                    exc,
                )
                continue
            closed += 1
        return ControlOutcome(
            completed=closed,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with signed qty for exposure limits and exit-only sizing.

        qty uses the same unit convention as signal metadata 'qty'
        (inverse of _resolve_order_volume: qty == broker volume / the
        symbol's volume_per_qty, #80).
        current_price/unrealized_pnl are not available without a spot
        subscription and are reported as 0.0.
        """
        open_positions = await self._fetch_open_positions()

        symbol_id_to_name = {
            symbol_id: name for name, symbol_id in self._symbol_name_to_id.items()
        }
        payload: list[dict[str, Any]] = []
        observed_position_ids: dict[str, list[int]] = {}
        observed_volumes: dict[int, int] = {}
        self._position_ids_by_symbol = {}
        self._position_volumes = observed_volumes
        for position in open_positions:
            row = self._position_row(position, symbol_id_to_name)
            if row is None:
                continue
            payload.append(row["row"])
            if row["position_id"] > 0:
                observed_position_ids.setdefault(row["symbol"], []).append(
                    row["position_id"]
                )
                observed_volumes[row["position_id"]] = row["volume"]
        self._position_ids_by_symbol = {
            symbol: tuple(position_ids)
            for symbol, position_ids in observed_position_ids.items()
        }
        return payload

    def _position_row(
        self, position: Any, symbol_id_to_name: Mapping[int, str]
    ) -> dict[str, Any] | None:
        """One ``get_positions`` row, or ``None`` when there is no trade data."""
        trade_data = getattr(position, "tradeData", None)
        if trade_data is None:
            return None
        symbol_id = int(getattr(trade_data, "symbolId", 0) or 0)
        # #80: unmapped ids fail closed (no guessed FX scale).
        symbol = symbol_id_to_name.get(symbol_id)
        if symbol is None:
            raise ValueError(
                f"cTrader symbol id {symbol_id!r} is not in the symbols "
                "cache; refusing to scale its position with a guess."
            )
        volume = parse_money(getattr(trade_data, "volume", 0))
        qty = _broker_volume_to_qty(
            volume, self._scale_for(symbol).volume_per_qty
        )
        if int(getattr(trade_data, "tradeSide", 0) or 0) == self._ProtoOATradeSide.SELL:
            qty = -qty
        normalized_symbol = str(symbol).strip().upper()
        # The flip workflow's fallback-TP removal verifies the broker TP
        # is the one it placed before clearing anything (#168 review):
        # the position read must carry the TP so a foreign TP fails
        # closed instead of being silently removed.
        broker_tp = (
            parse_optional(getattr(position, "takeProfit", None))
            if self._has_proto_field(position, "takeProfit")
            else None
        )
        position_id = int(getattr(position, "positionId", 0) or 0)
        row: dict[str, Any] = {
            "symbol": normalized_symbol,
            "qty": qty,
            "entry_price": parse_money(getattr(position, "price", 0)),
            "current_price": 0.0,
            "unrealized_pnl": 0.0,
            "take_profit": (
                float(broker_tp)
                if broker_tp is not None and broker_tp > 0
                else None
            ),
        }
        if position_id > 0:
            row["position_id"] = position_id
        return {
            "row": row,
            "symbol": normalized_symbol,
            "position_id": position_id if position_id > 0 else 0,
            "volume": int(volume),
        }

    # ── Protective stops / in-place SL-TP amendment (#168) ──────────────────────
    #
    # cTrader has no standalone protective-stop order: a position's stopLoss /
    # takeProfit live on the position itself and are amended atomically through
    # ProtoOAAmendPositionSLTPReq. The previous levels keep working until the
    # broker accepts the new ones, and a refused request changes nothing.
    # These primitives exist ONLY for the opt-in live ``supertrend_flip``
    # workflow on demo/paper accounts: the adapter deliberately does NOT
    # declare ``supports_stop_amendment``, so the percent-trailing path and
    # the broker reconciler treat every cTrader account exactly as on main.
    # ``supports_supertrend_flip`` is the single capability the flip manager
    # and the daemon's signal gates check, and it is demo-only.

    @property
    def supports_supertrend_flip(self) -> bool:
        """Live flip workflow requires a provably demo/paper account."""
        return self.demo is True

    async def list_protective_stops(self) -> list[ProtectiveStop]:
        """One amendable protective stop per open position that has one.

        Synthesized from ``ProtoOAPosition.stopLoss``: ``order_id`` is the
        broker ``positionId`` (there is no separate stop order to name), and
        ``side`` is the order side that would reduce the position - ``sell``
        for a long, ``buy`` for a short. Read from the broker on every call,
        so a restart continues from broker state.

        A position with no stop is simply not listed - the caller reports the
        missing protection. A position whose broker-native trailing stop is
        active is also not listed: that mechanism is not an absolute-price
        amend, so offering it would let a trail overwrite a level the broker
        manages itself. Anything structurally unreadable (no trade data, an
        unknown symbol or side) raises: a stop that cannot be named cannot be
        safely amended, and the whole listing failing is the loud answer.
        """
        open_positions = await self._fetch_open_positions()
        stops: list[ProtectiveStop] = []
        for position in open_positions:
            stop = self._position_protective_stop(position)
            if stop is not None:
                stops.append(stop)
        return stops

    def _position_protective_stop(self, position: Any) -> ProtectiveStop | None:
        """The amendable protective stop of one open position, or ``None``.

        ``None`` covers the two skip cases - a broker-native trailing stop
        (not an absolute-price amend) and a position carrying no stopLoss at
        all. Anything structurally unreadable raises instead: a stop that
        cannot be named cannot be safely amended.
        """
        position_id = self._positive_int(getattr(position, "positionId", 0))
        trade_data = getattr(position, "tradeData", None)
        if position_id is None or trade_data is None:
            raise ValueError(
                "cTrader open position is missing positionId or tradeData; "
                "refusing to report protective stops for an unattributable position"
            )
        if bool(getattr(position, "trailingStopLoss", False)):
            return None
        if not self._has_proto_field(position, "stopLoss"):
            return None
        stop_price = parse_optional(getattr(position, "stopLoss", None))
        if stop_price is None or stop_price <= 0:
            raise ValueError(
                f"cTrader position {position_id} carries an unreadable stopLoss "
                f"({getattr(position, 'stopLoss', None)!r}); refusing to guess it"
            )
        symbol_id = self._positive_int(getattr(trade_data, "symbolId", 0))
        if symbol_id is None:
            raise ValueError(
                f"cTrader position {position_id} has no readable symbolId; "
                "refusing to report its protective stop"
            )
        symbol = self._symbol_name_for_id(symbol_id).strip().upper()
        stop_side = self._stop_side_for_position(position_id, trade_data)
        qty: float | None = None
        try:
            volume = parse_money(getattr(trade_data, "volume", 0))
            qty = _broker_volume_to_qty(
                volume, self._scale_for(symbol).volume_per_qty
            )
        except (TypeError, ValueError):
            qty = None
        return ProtectiveStop(
            order_id=str(position_id),
            symbol=symbol,
            side=stop_side,
            stop_price=stop_price,
            qty=qty,
        )

    def _stop_side_for_position(self, position_id: int, trade_data: Any) -> str:
        """The order side that reduces this position - ``sell`` long, ``buy`` short."""
        trade_side = self._positive_int(getattr(trade_data, "tradeSide", 0))
        if trade_side == int(self._ProtoOATradeSide.BUY):
            return "sell"
        if trade_side == int(self._ProtoOATradeSide.SELL):
            return "buy"
        raise ValueError(
            f"cTrader position {position_id} has unreadable tradeSide "
            f"({getattr(trade_data, 'tradeSide', None)!r}); cannot tell "
            "which side its stop protects"
        )

    def round_stop_price(self, stop: ProtectiveStop, price: float) -> float:
        """Round a computed stop onto the symbol's price grid, away from market.

        Same convention as the Alpaca adapter: floor for a ``sell`` stop,
        ceiling for a ``buy`` stop, so the rounded stop is never tighter than
        the computed one.
        """
        value = float(price)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"Invalid stop price for {stop.symbol}: {price!r}; refusing to amend."
            )
        digits = self._scale_for(stop.symbol).price_digits
        tick = Decimal(1).scaleb(-max(0, int(digits)))
        # Float noise is removed first (101.96999999999998 is 101.97): flooring
        # the raw float would otherwise give away a whole tick to a rounding
        # artefact of the stop arithmetic.
        rounding = ROUND_FLOOR if stop.side == "sell" else ROUND_CEILING
        rounded = float(Decimal(repr(round(value, 8))).quantize(tick, rounding=rounding))
        if rounded <= 0:
            raise ValueError(
                f"Stop price for {stop.symbol} rounds to {rounded!r}; refusing to amend."
            )
        return rounded

    async def _fetch_open_position(self, position_id: int) -> Any | None:
        """The open ``ProtoOAPosition`` with this id, or ``None`` when closed."""
        for position in await self._fetch_open_positions():
            if self._positive_int(getattr(position, "positionId", 0)) == position_id:
                return position
        return None

    async def _confirm_position_stop(
        self, position_id: int, expected_price: float, *, digits: int
    ) -> float | None:
        """The broker-reported stop once it matches ``expected_price``.

        ``None`` while the broker has not yet shown the amended stop; the
        caller bounds the wait. A position that is gone returns a separate
        sentinel through the caller's own read - this only answers "does the
        open position carry the new stop".
        """
        position = await self._fetch_open_position(position_id)
        if position is None:
            return None
        if not self._has_proto_field(position, "stopLoss"):
            return None
        broker_price = parse_optional(getattr(position, "stopLoss", None))
        if broker_price is None or broker_price <= 0:
            return None
        tolerance = float(Decimal(1).scaleb(-max(0, int(digits)))) * 0.5 + 1e-12
        if math.isclose(broker_price, expected_price, rel_tol=0.0, abs_tol=tolerance):
            return broker_price
        return None

    async def amend_protective_stop(
        self, stop: ProtectiveStop, new_stop_price: float
    ) -> ProtectiveStop:
        """Move a position's stop loss to ``new_stop_price``, TP preserved.

        Sends ``ProtoOAAmendPositionSLTPReq`` with the new absolute stop and
        the position's current take profit echoed back unchanged (unset when
        the position has none, which the broker reads as "no TP" either way).
        The request has no dedicated response message, so the outcome is
        confirmed by re-reading the position: only a broker-reported stop at
        the requested price counts as amended. Any send failure that is not a
        definite broker refusal (timeout, dropped connection, malformed
        response) is verified the same way - if the state shows the new stop
        the amendment did land; if it cannot be confirmed this raises and the
        caller keeps treating the previous broker state as the truth.
        """
        position_id = self._positive_int(getattr(stop, "order_id", None))
        if position_id is None:
            raise ValueError(
                f"cTrader protective stop for {stop.symbol} has no usable "
                f"position id ({stop.order_id!r}); refusing to amend."
            )
        if self._ProtoOAAmendPositionSLTPReq is None:
            raise RuntimeError("cTrader amend-position-SLTP request type is not loaded")
        # Read the position first: it connects/authenticates (so the symbol
        # scale is broker-sourced), proves the position is still open, and
        # supplies the TP and protection flags to echo back unchanged.
        position = await self._fetch_open_position(position_id)
        if position is None:
            raise RuntimeError(
                f"cTrader position {position_id} ({stop.symbol}) is no longer open; "
                "there is no stop to amend."
            )
        digits = self._scale_for(stop.symbol).price_digits
        price = self.round_stop_price(stop, new_stop_price)

        request = self._build_amend_position_request(position_id, price, position)
        send_error = await self._send_amend_position_request(request)
        return await self._confirmed_amendment(
            position_id=position_id,
            price=price,
            digits=digits,
            stop=stop,
            send_error=send_error,
        )

    def _build_amend_position_request(
        self, position_id: int, price: float, position: Any
    ) -> Any:
        """The SLTP amendment: new absolute stop, TP and flags echoed back."""
        request = self._ProtoOAAmendPositionSLTPReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = position_id
        request.stopLoss = float(price)
        if self._has_proto_field(position, "takeProfit"):
            current_tp = parse_optional(getattr(position, "takeProfit", None))
            if current_tp is not None and current_tp > 0:
                request.takeProfit = float(current_tp)
        for field_name in (
            "guaranteedStopLoss",
            "trailingStopLoss",
            "stopLossTriggerMethod",
        ):
            if self._has_proto_field(position, field_name):
                setattr(request, field_name, getattr(position, field_name))
        return request

    async def _send_amend_position_request(self, request: Any) -> BaseException | None:
        """Send the amendment; the send error when the outcome is ambiguous.

        ``None`` means the broker answered without an order-error event. A
        definite ``CTraderApiError`` refusal propagates (the broker changed
        nothing); anything else - timeout, transport failure, malformed
        response - comes back as the exception for the caller to resolve
        against broker state.
        """
        try:
            response = await self._send_request_with_refresh_retry(
                request, timeout_seconds=10
            )
            self._raise_if_order_error_event(response)
        except CTraderApiError as exc:
            # A definite refusal: the broker changed nothing, the previous
            # stop is still in force.
            self._raise_terminal_auth_from_api_error(exc)
            raise
        except Exception as exc:  # except-kind: money-decision
            # Timeout, transport failure or an unparseable response: the
            # request may or may not have landed. Broker state decides below.
            return exc
        return None

    async def _confirmed_amendment(
        self,
        *,
        position_id: int,
        price: float,
        digits: int,
        stop: ProtectiveStop,
        send_error: BaseException | None,
    ) -> ProtectiveStop:
        """Poll broker state until the new stop shows, or fail closed."""
        confirmed_price: float | None = None
        for attempt in range(_AMEND_CONFIRM_ATTEMPTS):
            try:
                confirmed_price = await self._confirm_position_stop(
                    position_id, price, digits=digits
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # except-kind: money-decision
                confirmed_price = None
            if confirmed_price is not None:
                break
            if attempt + 1 < _AMEND_CONFIRM_ATTEMPTS:
                await asyncio.sleep(_AMEND_CONFIRM_INTERVAL_SECONDS)
        if confirmed_price is None:
            position_now = await self._fetch_open_position(position_id)
            if position_now is None:
                raise RuntimeError(
                    f"cTrader position {position_id} ({stop.symbol}) closed while its "
                    "stop was being amended; the amendment outcome is moot but the "
                    "close itself must be reconciled."
                )
            detail = f" send error: {send_error!r}" if send_error is not None else ""
            raise RuntimeError(
                f"cTrader did not confirm the stop amendment for position "
                f"{position_id} ({stop.symbol}): broker still reports stopLoss "
                f"{getattr(position_now, 'stopLoss', None)!r}, requested {price!r}.{detail}"
            )
        return ProtectiveStop(
            order_id=str(position_id),
            symbol=stop.symbol,
            side=stop.side,
            stop_price=confirmed_price,
            qty=stop.qty,
        )

    async def amend_position_take_profit(
        self, position_id: int, take_profit: float | None
    ) -> float | None:
        """Set (or clear) a position's take profit; the stop is preserved exactly.

        ``take_profit=None`` removes the TP; a price sets it. The position's
        current ``stopLoss`` is echoed back verbatim into the same atomic
        ``ProtoOAAmendPositionSLTPReq``, so the stop can never be dropped or
        loosened by this call - and a position whose stop cannot be read is
        refused outright. Exists for the flip workflow's fallback TP (#168):
        placed when the workflow halts or gives up so the position keeps an
        upside exit, and amended away again before the workflow resumes.

        Confirmed by re-reading the position: only a broker-reported
        ``takeProfit`` at the requested level (or absent/zero for a clear)
        counts. Same ambiguity rule as :meth:`amend_protective_stop`: a send
        failure that is not a definite broker refusal is verified against
        broker state, and an unconfirmed outcome raises.
        """
        pid = self._positive_int(position_id)
        if pid is None:
            raise ValueError(
                f"cTrader position id {position_id!r} is not usable; "
                "refusing to amend its take profit."
            )
        if self._ProtoOAAmendPositionSLTPReq is None:
            raise RuntimeError("cTrader amend-position-SLTP request type is not loaded")
        position, symbol, digits, stop_price, current_tp = (
            await self._position_tp_amend_context(pid)
        )
        target = (
            self._round_take_profit(symbol, take_profit)
            if take_profit is not None
            else None
        )
        tolerance = float(Decimal(1).scaleb(-max(0, int(digits)))) * 0.5 + 1e-12
        noop, noop_result = self._tp_amend_noop(target, current_tp, tolerance)
        if noop:
            return noop_result

        request = self._build_position_tp_request(pid, position, stop_price, target)
        send_error = await self._send_amend_position_request(request)
        confirmed = await self._confirmed_tp_amendment(
            position_id=pid,
            symbol=symbol,
            expected=target,
            digits=digits,
            expected_stop=stop_price,
            send_error=send_error,
        )
        return None if target is None else float(confirmed)

    async def _position_tp_amend_context(
        self, pid: int
    ) -> tuple[Any, str, int, float, float | None]:
        """``(position, symbol, price_digits, stop, current TP)`` for an amend.

        Every refusal lives here: a gone position, an unreadable symbolId,
        or a missing/non-positive ``stopLoss`` (the amend echoes the stop
        verbatim, so without a readable one it could clear protection).
        """
        position = await self._fetch_open_position(pid)
        if position is None:
            raise RuntimeError(
                f"cTrader position {pid} is no longer open; "
                "there is no take profit to amend."
            )
        trade_data = getattr(position, "tradeData", None)
        symbol_id = (
            self._positive_int(getattr(trade_data, "symbolId", 0))
            if trade_data is not None
            else None
        )
        if symbol_id is None:
            raise ValueError(
                f"cTrader position {pid} has no readable symbolId; "
                "refusing to amend its take profit."
            )
        symbol = self._symbol_name_for_id(symbol_id).strip().upper()
        digits = self._scale_for(symbol).price_digits
        stop_price = (
            parse_optional(getattr(position, "stopLoss", None))
            if self._has_proto_field(position, "stopLoss")
            else None
        )
        if stop_price is None or stop_price <= 0:
            raise ValueError(
                f"cTrader position {pid} ({symbol}) has no readable stopLoss "
                "to preserve; refusing to amend its take profit."
            )
        current_tp = (
            parse_optional(getattr(position, "takeProfit", None))
            if self._has_proto_field(position, "takeProfit")
            else None
        )
        return position, symbol, digits, stop_price, current_tp

    @staticmethod
    def _tp_amend_noop(
        target: float | None, current_tp: float | None, tolerance: float
    ) -> tuple[bool, float | None]:
        """``(handled, result)``: True when the broker already matches - no write."""
        if target is None:
            if current_tp is None or current_tp <= 0:
                return True, None  # already clear: idempotent, no write
            return False, None
        if (
            current_tp is not None
            and current_tp > 0
            and math.isclose(current_tp, target, rel_tol=0.0, abs_tol=tolerance)
        ):
            return True, float(current_tp)  # already at the target: no write
        return False, None

    def _round_take_profit(self, symbol: str, price: float) -> float:
        """Quantize a take-profit onto the symbol's price grid (nearest tick)."""
        value = float(price)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"Invalid take-profit price for {symbol}: {price!r}; "
                "refusing to amend."
            )
        digits = self._scale_for(symbol).price_digits
        tick = Decimal(1).scaleb(-max(0, int(digits)))
        rounded = float(
            Decimal(repr(round(value, 8))).quantize(tick, rounding=ROUND_HALF_UP)
        )
        if rounded <= 0:
            raise ValueError(
                f"Take-profit price for {symbol} rounds to {rounded!r}; "
                "refusing to amend."
            )
        return rounded

    def _build_position_tp_request(
        self,
        position_id: int,
        position: Any,
        stop_price: float,
        take_profit: float | None,
    ) -> Any:
        """The SLTP amendment: stop echoed verbatim, TP set or cleared."""
        request = self._ProtoOAAmendPositionSLTPReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = position_id
        request.stopLoss = float(stop_price)
        if take_profit is not None:
            request.takeProfit = float(take_profit)
        for field_name in (
            "guaranteedStopLoss",
            "trailingStopLoss",
            "stopLossTriggerMethod",
        ):
            if self._has_proto_field(position, field_name):
                setattr(request, field_name, getattr(position, field_name))
        return request

    async def _confirm_position_tp(
        self,
        position_id: int,
        expected: float | None,
        *,
        digits: int,
        expected_stop: float,
    ) -> float | None:
        """The broker-reported TP once it matches ``expected`` AND the stop survived.

        ``expected=None`` confirms an absent/zero ``takeProfit`` and reports
        ``0.0``; ``None`` means "not yet" (or the position is gone - the
        caller's final read distinguishes the two, same as the stop path).
        The amend echoes ``stopLoss`` verbatim, so confirmation also requires
        the broker stop to still sit at ``expected_stop`` within tick
        tolerance: a TP that landed while the stop was dropped or moved is
        NOT a confirmed amendment.
        """
        position = await self._fetch_open_position(position_id)
        if position is None:
            return None
        broker_tp = (
            parse_optional(getattr(position, "takeProfit", None))
            if self._has_proto_field(position, "takeProfit")
            else None
        )
        tp_ok = (
            (broker_tp is None or broker_tp <= 0)
            if expected is None
            else (
                broker_tp is not None
                and broker_tp > 0
                and math.isclose(
                    broker_tp,
                    expected,
                    rel_tol=0.0,
                    abs_tol=float(Decimal(1).scaleb(-max(0, int(digits)))) * 0.5
                    + 1e-12,
                )
            )
        )
        if not tp_ok:
            return None
        broker_stop = (
            parse_optional(getattr(position, "stopLoss", None))
            if self._has_proto_field(position, "stopLoss")
            else None
        )
        if broker_stop is None or not math.isclose(
            broker_stop,
            expected_stop,
            rel_tol=0.0,
            abs_tol=float(Decimal(1).scaleb(-max(0, int(digits)))) * 0.5 + 1e-12,
        ):
            return None
        if expected is None:
            return 0.0
        return float(broker_tp) if broker_tp is not None else None

    async def _confirmed_tp_amendment(
        self,
        *,
        position_id: int,
        symbol: str,
        expected: float | None,
        digits: int,
        expected_stop: float,
        send_error: BaseException | None,
    ) -> float:
        """Poll broker state until the TP shows (or clears), or fail closed."""
        confirmed: float | None = None
        for attempt in range(_AMEND_CONFIRM_ATTEMPTS):
            try:
                confirmed = await self._confirm_position_tp(
                    position_id,
                    expected,
                    digits=digits,
                    expected_stop=expected_stop,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # except-kind: money-decision
                confirmed = None
            if confirmed is not None:
                break
            if attempt + 1 < _AMEND_CONFIRM_ATTEMPTS:
                await asyncio.sleep(_AMEND_CONFIRM_INTERVAL_SECONDS)
        if confirmed is None:
            position_now = await self._fetch_open_position(position_id)
            if position_now is None:
                raise RuntimeError(
                    f"cTrader position {position_id} ({symbol}) closed while its "
                    "take profit was being amended; the amendment outcome is moot "
                    "but the close itself must be reconciled."
                )
            detail = f" send error: {send_error!r}" if send_error is not None else ""
            raise RuntimeError(
                f"cTrader did not confirm the take-profit amendment for position "
                f"{position_id} ({symbol}): broker still reports takeProfit "
                f"{getattr(position_now, 'takeProfit', None)!r} and stopLoss "
                f"{getattr(position_now, 'stopLoss', None)!r}, requested TP "
                f"{expected!r} with stop {expected_stop!r}.{detail}"
            )
        return confirmed

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
                    # mypy's `to_thread` signature demands a callable returning
                    # non-Optional (`Callable[[str, str], str]`) and rejects this
                    # one's `str | None` result; the lookup genuinely returns
                    # None for an unknown order and the `or ""` below handles
                    # it. The arguments themselves are both str.
                    find_live_signal_id_by_broker_order_id,  # type: ignore[arg-type]
                    self.account_config_id,
                    str(order_id),
                )
                or ""
            )

        signal_id = self._parse_client_order_id(
            client_order_id, order_id=order_id, execution_type=execution_type
        )
        if signal_id is None:
            # A ClosePositionReq has no clientOrderId of ours: the fill's id
            # is SDK-assigned, so the broker order id the submit persisted is
            # the attribution. Only an exit signal may claim a position-close
            # fill this way - an entry order's id on a close deal still takes
            # the broker-close path, which resolves the real opener.
            signal_id = await self._exit_signal_uuid_for_order_id(
                order_id,
                position_id=self._position_id_from_payload(order, deal),
            )
        if signal_id is None:
            if self._close_position_fill_pending(order=order, deal=deal):
                raise _PendingCloseFillRetentionError(
                    "cTrader fill closes a position whose own close order is "
                    "still in flight; retaining the event for reconciliation."
                )
            # #144: a broker-side position close (SL/TP, flatten, manual) names
            # no signal of ours, but its deal still resolves to the position's
            # opening order and through it to the parent signal's ledger. Only
            # an unambiguous chain is booked; anything else is flagged for
            # reconciliation, never guessed into a ledger.
            if await self._try_record_position_close(
                order=order, deal=deal, order_id=order_id, execution_type=execution_type
            ):
                return
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
        # The order's `executedVolume` is *cumulative* for the order, not this fill's slice, and
        # it is the only quantity the broker states that lets `save_live_execution_up_to_cumulative`
        # tell a redelivery from an amendment. Converted with the same symbol scale as the fill so
        # the cap and the stored rows are in one unit.
        cumulative_volume = self._resolve_order_cumulative_volume(order)
        cumulative_qty = (
            _broker_volume_to_qty(
                cumulative_volume, self._scale_for(fill_symbol).volume_per_qty
            )
            if cumulative_volume > 0
            else 0.0
        )
        execution = LiveExecution(
            signal_id=signal_id,
            actual_price=self._resolve_execution_price(order=order, deal=deal),
            # Stored beside the price, because `0.0` alone cannot say whether the broker omitted
            # the field, sent zero, or sent something unusable. Without it every consumer of
            # `actual_price` treats a non-answer as a price.
            actual_price_observed=self._execution_price_presence(order=order, deal=deal),
            filled_qty=filled_qty,
            commission=_deal_commission(
                deal,
                rebate_logged=getattr(self, "_rebate_logged_deal_ids", None),
            ),
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=str(order_id or ""),
            dedupe_key=self._build_dedupe_key(
                deal,
                order_id=order_id,
                timestamp_ms=timestamp_ms,
                filled_volume=filled_volume,
                order_cumulative_volume=cumulative_volume,
            ),
            # The broker's own per-fill id, so a later collision between two equal-sized fills is
            # decidable from the table rather than by taking the dedupe key apart again. `None`
            # when the broker sent no deal - the case the key builder handles by deriving a key.
            # `_deal_id_or_none` keeps the protobuf `0` sentinel out of the column, so "the broker
            # told us nothing" stays distinguishable from "the broker said zero".
            broker_fill_id=_deal_id_or_none(deal),
            order_type=self._order_type_name(order),
            requested_qty=self._requested_order_qty(order, fill_symbol),
        )
        if cumulative_qty > 0:
            # P1's amendment semantics, not a second insert: the same key with a *larger*
            # quantity amends the stored row, the same quantity is a redelivery, and nothing is
            # stored past the broker's own cumulative. That last part is what stops a repeated
            # order-level snapshot - whose volume is cumulative, not incremental - from being
            # added to the rows already stored for the signal.
            persisted_qty = await asyncio.to_thread(
                save_live_execution_up_to_cumulative, execution, cumulative_qty
            )
            persisted = persisted_qty > 0.0
        else:
            persisted = await asyncio.to_thread(save_live_execution, execution)
        if not persisted:
            # Either a redelivery of a fill already stored, or - when deals arrive out of order -
            # a fill the broker's cumulative cap has no room for yet (a later deal with a larger
            # cumulative was stored first). Nothing is lost in the second case: reconciliation
            # re-reads the order with its final cumulative and stores the missing slice.
            logger.info(
                "Skipped cTrader execution fill (duplicate, or beyond the broker cumulative so "
                "far; reconciliation recovers the latter)",
                extra={
                    "data": {
                        "signal_id": str(execution.signal_id),
                        "broker_order_id": execution.broker_order_id,
                        "execution_type": execution_type,
                        "dedupe_key": execution.dedupe_key,
                        "broker_fill_id": execution.broker_fill_id,
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

    # ─── broker-side position closes (#144) ────────────────────────────────────────────────

    @staticmethod
    def _has_proto_field(payload: Any, name: str) -> bool:
        """``HasField`` when the payload is a real protobuf, attribute presence otherwise."""
        has_field = getattr(payload, "HasField", None)
        if callable(has_field):
            try:
                return bool(has_field(name))
            except (TypeError, ValueError):
                return False
        return getattr(payload, name, None) is not None

    @staticmethod
    def _event_is_position_close(order: Any | None, deal: Any | None) -> bool:
        """Whether this execution event reduces an existing position.

        ``closePositionDetail`` is set on a deal that closed position volume -
        an SL/TP trigger, a flatten, a manual close, and this platform's own
        ``ClosePositionReq`` exits all carry it. ``closingOrder`` on the order
        is the same fact from the order side. Our own exits never reach this
        check: their order id resolves to the persisted exit signal first.
        """
        if deal is not None and CTraderLiveAdapter._has_proto_field(
            deal, "closePositionDetail"
        ):
            return True
        return order is not None and bool(getattr(order, "closingOrder", False))

    def _position_id_from_payload(self, order: Any | None, deal: Any | None) -> int | None:
        """The position a close deal/order belongs to, deal first."""
        for payload in (deal, order):
            if payload is None:
                continue
            position_id = self._positive_int(getattr(payload, "positionId", 0))
            if position_id is not None:
                return position_id
        return None

    #: How long a position's order list may be reused by the exit-target
    #: resolver. The TTL alone cannot prove a netted position's opener set is
    #: unchanged - a second opening order can appear at any time - so a cache
    #: hit is only honoured while the position's broker volume still equals
    #: the volume recorded at fetch time (``_position_volumes``, refreshed by
    #: ``get_positions``). Any fill moves the volume and forces a refetch;
    #: a cached list may therefore never be the stale proof that pins a lot
    #: to a shared position (#382 maintainer decision).
    _POSITION_ORDERS_CACHE_TTL_SECONDS = 300.0
    _POSITION_ORDERS_CACHE_MAX = 512

    async def _position_orders_for_exit_target(self, position_id: int) -> Any | None:
        """``_fetch_position_orders`` with a bounded, volume-checked cache (#382).

        Only successful reads are cached; an unreadable list stays a fresh
        failure every call so a transient broker error cannot pin a stale
        answer. The cache is keyed on the broker position id, which is never
        reused for a different position, and an entry is only reused while
        both the TTL holds and the position's volume - as last reported by
        ``get_positions`` - still matches the volume seen at fetch time.
        """
        key = int(position_id)
        cached = self._position_orders_cache.get(key)
        if cached is not None:
            fetched_at, orders, volume_at_fetch = cached
            if (
                time.monotonic() - fetched_at
                < self._POSITION_ORDERS_CACHE_TTL_SECONDS
                and self._position_volumes.get(key) == volume_at_fetch
            ):
                return orders
            del self._position_orders_cache[key]
        orders = await self._fetch_position_orders(position_id)
        if orders is not None:
            if len(self._position_orders_cache) >= self._POSITION_ORDERS_CACHE_MAX:
                self._position_orders_cache.clear()
            self._position_orders_cache[key] = (
                time.monotonic(),
                orders,
                self._position_volumes.get(key),
            )
        return orders

    async def _fetch_position_orders(
        self,
        position_id: int,
        *,
        from_timestamp_ms: int | None = None,
        to_timestamp_ms: int | None = None,
        pace: dict[str, Any] | None = None,
    ) -> Any | None:
        """Every order the broker ties to one position, opening and closing.

        ``ProtoOAOrderListByPositionIdReq`` accepts the same optional
        ``fromTimestamp``/``toTimestamp`` bounds as the account order list. The
        reconcile pass passes the open-exposure window explicitly: an opening
        order can be months older than a default "recent" lookup, and a missed
        opener reads as ``no_opening_order`` (a false ledger flag) instead of an
        attribution. ``pace`` is the reconcile pass's shared request pacer;
        the stream path leaves it ``None``.
        """
        if (
            self._ProtoOAOrderListByPositionIdReq is None
            or self._ProtoOAOrderListByPositionIdRes is None
        ):
            return None
        if pace is not None:
            await self._pace_broker_read(pace)
        request = self._ProtoOAOrderListByPositionIdReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = int(position_id)
        if from_timestamp_ms is not None:
            request.fromTimestamp = int(from_timestamp_ms)
        if to_timestamp_ms is not None:
            request.toTimestamp = int(to_timestamp_ms)
        response = await self._send_request_with_refresh_retry(request, timeout_seconds=15)
        if not isinstance(response, self._ProtoOAOrderListByPositionIdRes):
            logger.warning(
                "Unexpected order-list-by-position response during broker-close attribution",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "position_id": position_id,
                        "response_type": type(response).__name__,
                    }
                },
            )
            return None
        if getattr(response, "hasMore", False):
            # The response is sorted newest-first, so the opening order - the
            # oldest row - is exactly the one a ``hasMore`` cut would drop.
            # The window bounds keep this rare; make it loud when it happens.
            logger.warning(
                "cTrader order list by position reports hasMore; the oldest "
                "orders (possibly the opener) may be missing",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "position_id": position_id,
                        "from_timestamp_ms": from_timestamp_ms,
                        "to_timestamp_ms": to_timestamp_ms,
                    }
                },
            )
        return response

    async def _signal_id_for_position_order(self, order: Any) -> str | None:
        """The persisted signal one position order belongs to, or ``None``.

        The opening order carries our ``clientOrderId`` (the signal id) or, after
        a restart where the in-memory maps are empty, an order id the database
        can resolve. A ``clientOrderId`` only counts when it names a signal on
        THIS account: it is a bare UUID, and booking a close against another
        account's deployment would corrupt that ledger.
        """
        client_order_id = str(getattr(order, "clientOrderId", "") or "").strip()
        candidate = ""
        if client_order_id:
            try:
                candidate = str(UUID(client_order_id))
            except (ValueError, AttributeError, TypeError):
                candidate = ""
            if candidate:
                account_id = await asyncio.to_thread(
                    get_live_signal_account_id, candidate
                )
                if account_id is None or str(account_id) != str(
                    self.account_config_id
                ):
                    candidate = ""
        if not candidate:
            order_id = self._positive_int(getattr(order, "orderId", 0))
            if order_id is not None:
                resolved = self._order_id_to_signal_id.get(order_id)
                if resolved is None:
                    resolved = await asyncio.to_thread(
                        find_live_signal_id_by_broker_order_id,
                        self.account_config_id,
                        str(order_id),
                    )
                candidate = str(resolved) if resolved else ""
        if not candidate:
            return None
        # An exit signal is itself a close, never the position's opener: if the
        # broker did not flag its order ``closingOrder`` it must still not be
        # booked as the parent, or the close would reduce the wrong ledger side.
        metadata = await asyncio.to_thread(get_live_signal_metadata, candidate)
        if is_exit_only_signal_metadata(metadata or {}):
            return None
        return candidate

    async def _resolve_position_parent_signal(
        self,
        position_id: int,
        *,
        order_window: tuple[int, int] | None = None,
        pace: dict[str, Any] | None = None,
    ) -> tuple[str | None, str]:
        """``(parent_signal_id, cause)`` for the position's opening order(s).

        Exactly one distinct persisted signal may open the position for the
        close to be attributable. Zero (a manual position, or orders this
        platform never submitted) and several (a netted book several
        deployments share) are both refusals - the caller flags the ledger
        rather than guessing which shares the close took.

        Known limitation, kept deliberately fail-closed: several opening
        orders that all resolve to signals of ONE deployment on ONE side are
        still flagged ``ambiguous_parent``. The ledger is per deployment, so
        attribution would be exact there - but whether a scaled-in position's
        close belongs to that deployment alone is a maintainer decision
        (#277), and until it lands the flag is the honest answer.
        """
        from_timestamp_ms = order_window[0] if order_window is not None else None
        to_timestamp_ms = order_window[1] if order_window is not None else None
        orders = await self._fetch_position_orders(
            position_id,
            from_timestamp_ms=from_timestamp_ms,
            to_timestamp_ms=to_timestamp_ms,
            pace=pace,
        )
        if orders is None:
            return None, "position_orders_unreadable"
        candidates: set[str] = set()
        for order in getattr(orders, "order", []) or []:
            if bool(getattr(order, "closingOrder", False)):
                continue
            signal_id = await self._signal_id_for_position_order(order)
            if signal_id:
                candidates.add(signal_id)
        if len(candidates) == 1:
            return next(iter(candidates)), "ok"
        if not candidates:
            return None, "no_opening_order"
        return None, "ambiguous_parent"

    async def opening_signal_id_for_position(self, position_id: int) -> str | None:
        """Public provenance read for the flip manager's first-sight bind (#168).

        Reuses the broker-close attribution path: exactly one persisted
        opening signal may claim the position. ``None`` means proven
        foreign/ambiguous - the broker read the position's orders and none
        (or several) resolve to a persisted signal. An UNREADABLE order list
        raises ``CTraderApiError`` instead: the caller retries a transient
        read failure rather than treating it as proven foreign ownership.
        """
        signal_id, cause = await self._resolve_position_parent_signal(
            int(position_id)
        )
        if cause == "position_orders_unreadable":
            raise CTraderApiError(
                "ORDER_LIST_UNREADABLE",
                f"cTrader position orders for position {position_id} are "
                "unreadable; provenance cannot be proven this pass",
            )
        return signal_id

    @staticmethod
    def _broker_close_dedupe_key(deal: Any | None) -> str | None:
        """The one dedupe derivation every path shares for one deal.

        ``dealId`` is unique per fill by construction, and it is the SAME key
        ``_build_dedupe_key`` derives for the normal fill path: a deal that
        reaches both paths - booked as a broker-side close, then resolved to a
        signal once the exit's broker order id is persisted - must collide on
        one key, or the ledger is reduced twice. A close deal without a dealId
        cannot be told from its own redelivery, so it is refused rather than
        keyed on something that could count it twice.
        """
        deal_id = _deal_id_or_none(deal)
        if deal_id is None:
            return None
        return f"ctrader:deal:{deal_id}"

    def _close_side_from_payload(self, order: Any | None, deal: Any | None) -> str | None:
        """``buy``/``sell`` of the closing deal, for the insert's side cross-check."""
        trade_side = None
        for payload in (deal, order):
            if payload is None:
                continue
            value = getattr(payload, "tradeSide", None)
            if value:
                trade_side = value
                break
        if trade_side is None or self._ProtoOATradeSide is None:
            return None
        try:
            side_value = int(trade_side)
        except (TypeError, ValueError):
            return None
        if side_value == int(self._ProtoOATradeSide.BUY):
            return "buy"
        if side_value == int(self._ProtoOATradeSide.SELL):
            return "sell"
        return None

    def _fill_symbol_name(
        self, *, order_id: int | None, order: Any | None, deal: Any | None
    ) -> str | None:
        """The upper symbol name a close fill trades, or ``None`` when unreadable.

        Same precedence as the normal fill path - deal ``symbolId``, then the
        order's, then the submit-time order map - but non-raising: the caller
        flags the ledger instead of dropping the event silently.
        """
        for payload in (deal, order):
            if payload is None:
                continue
            symbol_id = int(getattr(payload, "symbolId", 0) or 0)
            if symbol_id:
                try:
                    return self._symbol_name_for_id(symbol_id)
                except ValueError:
                    return None
        if order_id is not None:
            return self._order_id_to_symbol.get(int(order_id))
        return None

    async def _persist_position_close_fill(
        self,
        *,
        parent_signal_id: str,
        order: Any | None,
        deal: Any | None,
        order_id: int | None,
        dedupe_key: str,
    ) -> tuple[float, str | None]:
        """Book one broker-side close against the parent's ledger (#144).

        Returns ``(persisted_qty, failure_cause)``. ``failure_cause`` is ``None``
        both when quantity was persisted and when the fill was already recorded
        (a redelivery or a slice reconciliation booked first) - the caller only
        flags on a real ``failure_cause``. The closing order's cumulative
        executed volume caps the booking per broker order inside the insert,
        exactly like the Alpaca leg path; the deal's own ``filledVolume`` is the
        slice this fill contributes.
        """
        filled_volume = self._resolve_filled_volume(order=order, deal=deal)
        if filled_volume <= 0:
            return 0.0, "unusable_volume"
        symbol = self._fill_symbol_name(order_id=order_id, order=order, deal=deal)
        if symbol is None:
            return 0.0, "unusable_symbol"
        scale = self._scale_for(symbol)
        filled_qty = _broker_volume_to_qty(filled_volume, scale.volume_per_qty)
        timestamp_ms = self._resolve_execution_timestamp_ms(order=order, deal=deal)
        if timestamp_ms <= 0:
            # The ledger is ordered by fill time; an invented epoch stamp could
            # not be corrected later, so the fill is refused and flagged.
            return 0.0, "unusable_timestamp"
        cumulative_volume = self._resolve_order_cumulative_volume(order)
        cumulative_qty = (
            _broker_volume_to_qty(cumulative_volume, scale.volume_per_qty)
            if cumulative_volume > 0
            else None
        )
        persisted_qty = await asyncio.to_thread(
            record_live_broker_side_close_fill,
            parent_signal_id=parent_signal_id,
            close_reason=BROKER_CLOSE_REASON_CTRADER_POSITION_CLOSE,
            filled_qty=filled_qty,
            actual_price=self._resolve_execution_price(order=order, deal=deal),
            actual_price_observed=self._execution_price_presence(order=order, deal=deal),
            commission=_deal_commission(deal),
            timestamp=_to_datetime_from_ms(timestamp_ms),
            broker_order_id=str(order_id or ""),
            dedupe_key=dedupe_key,
            broker_fill_id=_deal_id_or_none(deal),
            close_side=self._close_side_from_payload(order, deal),
            cumulative_filled_qty=cumulative_qty,
        )
        if persisted_qty > 0.0:
            return persisted_qty, None
        if await asyncio.to_thread(live_execution_dedupe_key_exists, dedupe_key):
            # A redelivery, or a slice the reconcile pass already booked: the
            # fill is handled, just not by this call.
            return 0.0, None
        return 0.0, "record_refused"

    def _flag_ledger_reconciliation(
        self,
        symbol: str,
        cause: str,
        detail: Mapping[str, Any],
        *,
        flagged_ids: set[int] | None = None,
    ) -> None:
        """Surface an unattributable close: durable audit flag + ERROR alert.

        The fill is NOT guessed into a ledger (the #144 maintainer decision for
        flatten/manual closes). The audit row is what an operator and the
        periodic broker reconciliation (#330) can act on; the ERROR record is
        the alert channel.

        One flag per deal per process: the reconcile pass and the stream's
        retain-for-reconciliation retries see the same unattributable deal
        again and again, and each repeat would otherwise add another audit row
        and another ERROR alert. A deal with no id cannot be deduplicated and
        is flagged every time. ``flagged_ids`` lets the reconcile pass pass
        the durable set it already loaded instead of re-querying per flag.
        """
        deal_id = detail.get("deal_id")
        if isinstance(deal_id, int) and deal_id > 0:
            if deal_id in self._flagged_broker_close_deals:
                logger.info(
                    "cTrader broker-side close already flagged for reconciliation; "
                    "not flagging the same deal again",
                    extra={
                        "data": {
                            "account_id": self.account_config_id,
                            "symbol": symbol,
                            "cause": cause,
                            "deal_id": deal_id,
                        }
                    },
                )
                return
            known_flagged = (
                flagged_ids
                if flagged_ids is not None
                else list_live_ledger_reconciliation_deal_ids(
                    self.account_config_id
                )
            )
            if deal_id in known_flagged:
                self._flagged_broker_close_deals.add(deal_id)
                logger.info(
                    "cTrader broker-side close was already flagged for "
                    "reconciliation before this process started; not flagging "
                    "the same deal again",
                    extra={
                        "data": {
                            "account_id": self.account_config_id,
                            "symbol": symbol,
                            "cause": cause,
                            "deal_id": deal_id,
                        }
                    },
                )
                return
            self._flagged_broker_close_deals.add(deal_id)
        flag_live_ledger_reconciliation(
            account_id=self.account_config_id,
            symbol=symbol,
            cause=cause,
            detail=dict(detail),
        )
        logger.error(
            "ALERT: cTrader broker-side position close could not be attributed "
            "to one deployment; the account ledger needs reconciliation",
            extra={
                "data": {
                    "alert": "ledger_reconciliation_required",
                    "account_id": self.account_config_id,
                    "symbol": symbol,
                    "cause": cause,
                    **dict(detail),
                }
            },
        )

    async def _broker_close_order_window(self) -> tuple[int, int]:
        """The ``(from, to)`` ms window for opening-order lookups on a close.

        Same window the reconcile pass computes: the oldest open ledger lot
        minus ``BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS`` through ``now`` - an
        opener can be months older than any default "recent" order lookup,
        and a missed opener reads as a false ``no_opening_order`` flag. With
        no open exposure the window is the flat recent week, matching the
        reconcile floor.
        """
        now_ms = int(time.time() * 1000)
        open_lots = await asyncio.to_thread(
            list_live_account_open_position_lots, self.account_config_id
        )
        oldest_ms = self._oldest_open_exposure_ms(open_lots)
        from_ms = (
            int(oldest_ms) - BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS
            if oldest_ms is not None
            else now_ms - BROKER_CLOSE_RECONCILE_WINDOW_MS
        )
        return (max(0, int(from_ms)), now_ms)

    async def _try_record_position_close(
        self,
        *,
        order: Any | None,
        deal: Any | None,
        order_id: int | None,
        execution_type: int,
    ) -> bool:
        """Book an unmapped position-close fill, or flag why it cannot be.

        ``True`` means the event is handled (booked or already recorded) and may
        be acknowledged; ``False`` leaves the caller's retain-for-reconciliation
        path in charge.
        """
        if not self._event_is_position_close(order, deal):
            return False
        position_id = self._position_id_from_payload(order, deal)
        symbol = (
            self._fill_symbol_name(order_id=order_id, order=order, deal=deal) or ""
        )
        base_detail: dict[str, Any] = {
            "order_id": str(order_id or ""),
            "deal_id": _deal_id_or_none(deal),
            "execution_type": execution_type,
            "deal_timestamp_ms": self._resolve_execution_timestamp_ms(
                order=order, deal=deal
            ),
        }
        if position_id is None:
            self._flag_ledger_reconciliation(symbol, "no_position_id", base_detail)
            return False
        order_window = await self._broker_close_order_window()
        parent_signal_id, cause = await self._resolve_position_parent_signal(
            position_id, order_window=order_window
        )
        if parent_signal_id is None:
            self._flag_ledger_reconciliation(
                symbol, cause, {**base_detail, "position_id": position_id}
            )
            return False
        dedupe_key = self._broker_close_dedupe_key(deal)
        if dedupe_key is None:
            self._flag_ledger_reconciliation(
                symbol,
                "unusable_fill",
                {**base_detail, "position_id": position_id, "missing": "deal_id"},
            )
            return False
        persisted_qty, failure = await self._persist_position_close_fill(
            parent_signal_id=parent_signal_id,
            order=order,
            deal=deal,
            order_id=order_id,
            dedupe_key=dedupe_key,
        )
        if failure is not None:
            self._flag_ledger_reconciliation(
                symbol,
                failure,
                {
                    **base_detail,
                    "position_id": position_id,
                    "parent_signal_id": parent_signal_id,
                },
            )
            return False
        logger.info(
            (
                "Persisted cTrader broker-side position close fill"
                if persisted_qty > 0.0
                else "Skipped cTrader broker-side position close fill "
                "(already recorded)"
            ),
            extra={
                "data": {
                    "account_id": self.account_config_id,
                    "parent_signal_id": parent_signal_id,
                    "close_signal_id": broker_close_signal_id(parent_signal_id),
                    "position_id": position_id,
                    "order_id": str(order_id or ""),
                    "deal_id": _deal_id_or_none(deal),
                    "filled_qty": persisted_qty,
                }
            },
        )
        return True

    async def _fetch_order_details(
        self, order_id: int, *, pace: dict[str, Any] | None = None
    ) -> Any | None:
        """One order's details for the reconcile pass's cumulative cap."""
        if (
            self._ProtoOAOrderDetailsReq is None
            or self._ProtoOAOrderDetailsRes is None
        ):
            return None
        if pace is not None:
            await self._pace_broker_read(pace)
        request = self._ProtoOAOrderDetailsReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.orderId = int(order_id)
        try:
            response = await self._send_request_with_refresh_retry(
                request, timeout_seconds=15
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            logger.info(
                "cTrader order-details fetch failed during broker-close reconcile",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "order_id": order_id,
                        "error": str(exc),
                    }
                },
            )
            return None
        if not isinstance(response, self._ProtoOAOrderDetailsRes):
            return None
        order = getattr(response, "order", None)
        if isinstance(order, (list, tuple)):
            order = order[0] if order else None
        return order

    async def _reconcile_position_close_deal(
        self,
        deal: Any,
        *,
        parent_cache: dict[int, tuple[str | None, str]],
        order_cache: dict[int, Any | None],
        own_order_cache: dict[int, bool],
        flagged_deal_ids: set[int],
        order_window: tuple[int, int] | None = None,
        pace: dict[str, Any] | None = None,
        deferred_close_deals: list[Any],
    ) -> int:
        """Book one deal-list close the stream missed, or flag it (#144).

        ``flagged_deal_ids`` suppresses RE-FLAGGING only: a deal an earlier
        process already surfaced is still retried here, because a transient
        cause (unreadable order list, refused record, a too-narrow opener
        window) can resolve on a later pass. Booking it then supersedes the
        flag with a ``ledger_reconciliation_resolved`` audit row.

        ``deferred_close_deals`` collects deals skipped because their
        position's own close submit is still marked pending: the caller puts
        their windows back on the scan cursor so a later pass books them
        once the submit's broker order id is known.
        """
        if not self._event_is_position_close(None, deal):
            return 0
        deal_status = int(getattr(deal, "dealStatus", 0) or 0)
        if deal_status not in {
            self._ProtoOADealStatus.FILLED,
            self._ProtoOADealStatus.PARTIALLY_FILLED,
        }:
            return 0
        position_id = self._position_id_from_payload(None, deal)
        if position_id is None:
            return 0
        if self._resolve_filled_volume(order=None, deal=deal) <= 0:
            return 0
        deal_id = _deal_id_or_none(deal)
        if deal_id is None:
            return 0
        dedupe_key = self._broker_close_dedupe_key(deal)
        if dedupe_key is not None and await asyncio.to_thread(
            live_execution_dedupe_key_exists, dedupe_key
        ):
            # Already booked by the stream or an earlier pass: skip before the
            # position-order/order-detail broker reads, not after them. A
            # still-flagged deal is handled now, so its flag is stale - the
            # resolve itself only fires when every outstanding flag on the
            # target names an already-booked deal.
            if deal_id in flagged_deal_ids:
                await self._resolve_booked_ledger_flag(
                    self._fill_symbol_name(order_id=None, order=None, deal=deal)
                    or "",
                    deal_id,
                    parent_signal_id="",
                    persisted_qty=0.0,
                )
            return 0
        order_id = self._positive_int(getattr(deal, "orderId", 0))
        if order_id is not None:
            # A deal our own order made is the per-signal reconcile's job, not
            # this pass's: booking it here too would close the ledger twice.
            if order_id not in own_order_cache:
                own_order_cache[order_id] = (
                    self._order_id_to_signal_id.get(order_id) is not None
                )
                if not own_order_cache[order_id]:
                    owner_ids = await asyncio.to_thread(
                        find_live_signal_ids_by_broker_order_id,
                        self.account_config_id,
                        str(order_id),
                    )
                    if len(owner_ids) > 1:
                        # Ambiguous ownership - broker_order_id is indexed,
                        # not unique, so recovered duplicates can share one.
                        # The deal may be ours or a broker-side close and
                        # booking either way risks the wrong ledger side:
                        # flag it for an operator instead of guessing.
                        self._flag_ledger_reconciliation(
                            self._fill_symbol_name(
                                order_id=order_id, order=None, deal=deal
                            )
                            or "",
                            "ambiguous_broker_order_id",
                            {
                                "position_id": position_id,
                                "deal_id": deal_id,
                                "order_id": str(order_id),
                                "owner_signal_ids": owner_ids,
                                "deal_timestamp_ms": (
                                    self._resolve_execution_timestamp_ms(
                                        order=None, deal=deal
                                    )
                                ),
                                "source": "reconcile",
                            },
                            flagged_ids=flagged_deal_ids,
                        )
                        own_order_cache[order_id] = True
                        return 0
                    own_order_cache[order_id] = bool(owner_ids)
            if own_order_cache[order_id]:
                return 0
        if self._pending_close_position_active(position_id):
            # A ClosePositionReq submit for this position is still in flight
            # (or its outcome is unknown): this deal may be OUR close, whose
            # broker order id is not yet attributable - booking it here
            # would mis-book our own exit as a broker-side close, the exact
            # bug the pending mark exists to prevent. Defer the deal's
            # window instead of booking or flagging it.
            deferred_close_deals.append(deal)
            return 0
        if position_id not in parent_cache:
            parent_cache[position_id] = await self._resolve_position_parent_signal(
                position_id, order_window=order_window, pace=pace
            )
        parent_signal_id, cause = parent_cache[position_id]
        symbol = ""
        if parent_signal_id is None:
            symbol = (
                self._fill_symbol_name(order_id=order_id, order=None, deal=deal) or ""
            )
            self._flag_ledger_reconciliation(
                symbol,
                cause,
                {
                    "position_id": position_id,
                    "deal_id": deal_id,
                    "order_id": str(order_id or ""),
                    "deal_timestamp_ms": self._resolve_execution_timestamp_ms(
                        order=None, deal=deal
                    ),
                    "source": "reconcile",
                },
                flagged_ids=flagged_deal_ids,
            )
            return 0
        order = None
        if order_id is not None:
            if order_id not in order_cache:
                order_cache[order_id] = await self._fetch_order_details(
                    order_id, pace=pace
                )
            order = order_cache[order_id]
        if dedupe_key is None:
            return 0
        persisted_qty, failure = await self._persist_position_close_fill(
            parent_signal_id=parent_signal_id,
            order=order,
            deal=deal,
            order_id=order_id,
            dedupe_key=dedupe_key,
        )
        if failure is not None:
            symbol = (
                self._fill_symbol_name(order_id=order_id, order=order, deal=deal) or ""
            )
            self._flag_ledger_reconciliation(
                symbol,
                failure,
                {
                    "position_id": position_id,
                    "deal_id": deal_id,
                    "order_id": str(order_id or ""),
                    "deal_timestamp_ms": self._resolve_execution_timestamp_ms(
                        order=None, deal=deal
                    ),
                    "parent_signal_id": parent_signal_id,
                    "source": "reconcile",
                },
                flagged_ids=flagged_deal_ids,
            )
            return 0
        if deal_id in flagged_deal_ids:
            # The booking attempt succeeded for a deal an earlier process
            # flagged: supersede the flag as resolved-by-booking so the
            # account:symbol stops counting as unreconciled - but only when
            # every other outstanding flag on the target is booked too.
            await self._resolve_booked_ledger_flag(
                self._fill_symbol_name(order_id=order_id, order=order, deal=deal)
                or "",
                deal_id,
                parent_signal_id=parent_signal_id,
                persisted_qty=persisted_qty,
            )
        if persisted_qty > 0.0:
            logger.info(
                "Reconciled missed cTrader broker-side position close fill",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "parent_signal_id": parent_signal_id,
                        "close_signal_id": broker_close_signal_id(parent_signal_id),
                        "position_id": position_id,
                        "order_id": str(order_id or ""),
                        "deal_id": deal_id,
                        "filled_qty": persisted_qty,
                    }
                },
            )
            return 1
        return 0

    async def _resolve_booked_ledger_flag(
        self,
        symbol: str,
        deal_id: int,
        *,
        parent_signal_id: str,
        persisted_qty: float,
    ) -> None:
        """Supersede ``ledger_reconciliation_required`` flags as resolved-by-booking.

        The flag's audit row is never touched (the trail is append-only); a
        ``ledger_reconciliation_resolved`` row for the same ``account:symbol``
        target is what stops the flag counting as unresolved. Resolution is
        per account+symbol, NOT per deal, so it is only written when it is
        both needed and safe: every flag still outstanding on the target must
        name a deal the ledger has already booked, with a cause a booking can
        clear. One unattributable sibling, one deterministic flag
        (``ambiguous_parent``, ``no_position_id``, ``unusable_*``) or one
        flag without a deal id keeps the whole target unresolved - resolving
        anyway would silently clear flags this booking says nothing about
        and lift the loss-breaker halt with the ledger still wrong.
        """
        if not symbol or not self.account_config_id:
            return
        outstanding = await asyncio.to_thread(
            list_outstanding_ledger_flag_details,
            self.account_config_id,
            symbol,
        )
        if not outstanding:
            # Already resolved (or never flagged): writing another resolution
            # row would only grow the audit trail and re-supersede nothing.
            return
        required_keys: set[str] = set()
        for detail in outstanding:
            cause = str(detail.get("cause") or "")
            if cause in _LEDGER_FLAG_UNRESOLVABLE_CAUSES or cause.startswith(
                "unusable_"
            ):
                return
            try:
                flagged_deal_id = int(detail.get("deal_id") or 0)
            except (TypeError, ValueError):
                return
            if flagged_deal_id <= 0:
                return
            required_keys.add(f"ctrader:deal:{flagged_deal_id}")
        present = await asyncio.to_thread(
            live_execution_dedupe_keys_present, required_keys
        )
        if required_keys - present:
            return
        outcome = (
            f"booked {persisted_qty}"
            if persisted_qty > 0.0
            else "already recorded"
        )
        parent = f" under parent {parent_signal_id}" if parent_signal_id else ""
        booked_ids = ", ".join(sorted(required_keys))
        await asyncio.to_thread(
            resolve_ledger_reconciliation_flag,
            account_id=self.account_config_id,
            symbol=symbol,
            actor="live-runner",
            note=(
                f"cTrader broker-close deal {deal_id}{parent} {outcome} "
                f"by the broker-close reconcile; clears booked flags {booked_ids}"
            ),
        )

    async def _pace_broker_read(self, pacer: dict[str, Any]) -> None:
        """Space one broker read below the documented 5/s historical limit.

        ``pacer`` is a per-pass dict carrying ``last_request_at``; deal-list,
        order-list-by-position and order-details reads during a reconcile
        pass all share it, so the combined rate stays under the cap.
        """
        interval = float(self._broker_close_deal_list_min_interval_seconds or 0.0)
        if interval <= 0:
            return
        now = time.monotonic()
        last = float(pacer.get("last_request_at") or 0.0)
        earliest = last + interval if last > 0 else now
        if now < earliest:
            await asyncio.sleep(earliest - now)
        pacer["last_request_at"] = time.monotonic()

    async def _acquire_broker_close_deal_list_request(
        self, budget: dict[str, Any]
    ) -> bool:
        """Take one deal-list request slot and pace it below the broker limit."""
        if budget["exhausted"]:
            return False
        remaining = int(budget["remaining"])
        if remaining <= 0:
            budget["exhausted"] = True
            return False
        budget["remaining"] = remaining - 1
        await self._pace_broker_read(budget)
        return True

    async def _fetch_broker_close_deal_window(
        self,
        start_ms: int,
        end_ms: int,
        *,
        budget: dict[str, Any],
        depth: int = 0,
    ) -> list[Any] | None:
        """One deal-list window, halved while the broker reports ``hasMore``.

        ``None`` means this range could not be covered - the fetch failed or
        the request budget was already spent - and the caller keeps the range
        pending for a later pass. ``budget`` is the per-pass request
        cap/pacer, and ``depth`` bounds ``hasMore`` subdivision so a
        pathological account cannot turn one window into a request storm.
        """
        if not await self._acquire_broker_close_deal_list_request(budget):
            return None
        request = self._ProtoOADealListReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.fromTimestamp = int(start_ms)
        request.toTimestamp = int(end_ms)
        request.maxRows = BROKER_CLOSE_RECONCILE_MAX_ROWS
        try:
            response = await self._send_request_with_refresh_retry(
                request, timeout_seconds=20
            )
        except CTraderApiError as exc:
            self._raise_terminal_auth_from_api_error(exc)
            logger.info(
                "cTrader deal-list fetch failed during broker-close reconcile",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "from_ms": start_ms,
                        "to_ms": end_ms,
                        "error": str(exc),
                    }
                },
            )
            return None
        except Exception as exc:  # except-kind: money-decision
            is_timeout, _ = _classify_deferred_response_timeout(exc, str(exc))
            if not (
                is_timeout
                or isinstance(exc, (TimeoutError, ConnectionError, OSError))
            ):
                raise
            logger.info(
                "cTrader deal-list fetch failed during broker-close reconcile",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "from_ms": start_ms,
                        "to_ms": end_ms,
                        "error": str(exc),
                    }
                },
            )
            return None
        if not isinstance(response, self._ProtoOADealListRes):
            logger.warning(
                "Unexpected deal-list response during broker-close reconcile",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "from_ms": start_ms,
                        "to_ms": end_ms,
                        "response_type": type(response).__name__,
                    }
                },
            )
            return None
        deals = list(getattr(response, "deal", []) or [])
        if getattr(response, "hasMore", False):
            can_subdivide = (
                end_ms - start_ms > BROKER_CLOSE_RECONCILE_MIN_WINDOW_MS
                and depth < BROKER_CLOSE_RECONCILE_MAX_SUBDIVISION_DEPTH
                and not budget["exhausted"]
            )
            if can_subdivide:
                # maxRows truncated the window: page deeper instead of
                # accepting the miss. Halving keeps every sub-window under
                # the row cap where the account's deal rate allows it.
                mid_ms = start_ms + (end_ms - start_ms) // 2
                first = await self._fetch_broker_close_deal_window(
                    start_ms, mid_ms, budget=budget, depth=depth + 1
                )
                second = await self._fetch_broker_close_deal_window(
                    mid_ms, end_ms, budget=budget, depth=depth + 1
                )
                if first is None or second is None:
                    # A sub-range could not be fetched (timeout or the request
                    # budget ran out mid-subdivision). The parent's rows are
                    # still real coverage - keep them plus whichever half
                    # succeeded, and record each missing half as UNCOVERED so
                    # the scan cursor re-queues it next pass: a failed fetch
                    # is transient, and losing the range here would strand a
                    # close the old flat-window rescan used to heal.
                    gathered = list(deals)
                    if first is not None:
                        gathered.extend(first)
                    if second is not None:
                        gathered.extend(second)
                    if first is None:
                        budget["failed_ranges"].append(
                            (int(start_ms), int(mid_ms))
                        )
                    if second is None:
                        budget["failed_ranges"].append(
                            (int(mid_ms), int(end_ms))
                        )
                    return gathered
                return first + second
            # The window still reports more rows than one response can carry
            # and subdivision is exhausted. A spent request budget is
            # transient - the range goes back on the cursor. A depth or width
            # cap cannot be fixed by rescanning, so it is recorded as
            # unpageable for the pass-level warning/alert instead.
            if budget["exhausted"]:
                budget["failed_ranges"].append((int(start_ms), int(end_ms)))
            else:
                budget["truncated_ranges"].append((int(start_ms), int(end_ms)))
        return deals

    @staticmethod
    def _merge_scan_ranges(ranges: list[list[int]]) -> list[list[int]]:
        """Sort ``[from, to)`` deal-time ranges and merge overlapping ones.

        Invalid or empty ranges are dropped; the result is sorted by ``from``.
        """
        ordered = sorted(
            (int(start), int(end))
            for start, end in ranges
            if int(end) > int(start)
        )
        merged: list[list[int]] = []
        for start, end in ordered:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return merged

    async def _broker_close_scan_pending_ranges(
        self, *, floor_ms: int, now_ms: int
    ) -> list[list[int]]:
        """The deal-time ranges this pass still has to cover, newest first.

        The persisted cursor says which ``[from, to)`` ranges earlier passes
        left uncovered; this pass adds the fresh gap since the last covered
        top (overlapped by ``BROKER_CLOSE_SCAN_CURSOR_OVERLAP_MS`` so a deal
        the deal list had not yet shown is not skipped forever), any band a
        moved floor reopened, and a small window around every
        transiently-flagged deal whose fill is not already booked, so its
        booking attempt is retried even when its original window is already
        covered. Ranges are clamped to ``[floor_ms, now_ms]`` and merged
        before scanning.
        """
        try:
            state = (
                await asyncio.to_thread(
                    get_live_adapter_state,
                    self.account_config_id,
                    BROKER_CLOSE_SCAN_STATE_KEY,
                )
                or {}
            )
        except Exception as exc:  # except-kind: money-decision
            # A state read failure must not abort the pass: degrade to the
            # full floor..now scan, which is what a missing cursor means.
            logger.warning(
                "cTrader broker-close scan cursor unreadable; scanning the "
                "full range this pass",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "error": str(exc),
                    }
                },
            )
            state = {}
        covered_top = state.get("top_ms")
        covered_floor = state.get("floor_ms")
        try:
            covered_top_ms = int(covered_top) if covered_top is not None else 0
        except (TypeError, ValueError):
            covered_top_ms = 0
        try:
            covered_floor_ms = (
                int(covered_floor) if covered_floor is not None else 0
            )
        except (TypeError, ValueError):
            covered_floor_ms = 0
        pending: list[list[int]] = []
        if covered_top_ms <= 0:
            # No completed pass on record: the whole floor..now band is open.
            pending.append([floor_ms, now_ms])
        else:
            if covered_floor_ms > floor_ms:
                # The floor moved older: the band between them was never
                # scanned and is now inside the lookback again.
                pending.append([floor_ms, covered_floor_ms])
            elif covered_floor_ms <= 0:
                # A cursor without a floor claim proves nothing below its
                # top: rescan the band rather than trust it.
                pending.append([floor_ms, covered_top_ms])
            if covered_top_ms < now_ms:
                # The overlap re-reads a sliver below the last covered top:
                # a deal the deal list had not yet shown, or one stamped
                # before it by broker/local clock skew, still gets fetched.
                pending.append(
                    [
                        covered_top_ms - BROKER_CLOSE_SCAN_CURSOR_OVERLAP_MS,
                        now_ms,
                    ]
                )
        try:
            flag_entries = await asyncio.to_thread(
                list_live_ledger_reconciliation_flag_entries,
                self.account_config_id,
            )
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "cTrader broker-close flag retry lookup failed; flagged "
                "deals are not re-queued this pass",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "error": str(exc),
                    }
                },
            )
            flag_entries = []
        flagged_windows: list[tuple[int, list[int]]] = []
        for entry in flag_entries:
            detail = entry.get("detail")
            if not isinstance(detail, dict):
                continue
            if (
                str(detail.get("cause") or "")
                not in BROKER_CLOSE_FLAGGED_RETRYABLE_CAUSES
            ):
                continue
            try:
                flag_deal_id = int(detail.get("deal_id") or 0)
            except (TypeError, ValueError):
                flag_deal_id = 0
            try:
                deal_ts_ms = int(detail.get("deal_timestamp_ms") or 0)
            except (TypeError, ValueError):
                deal_ts_ms = 0
            if deal_ts_ms <= 0:
                # Legacy flags predate ``deal_timestamp_ms``: the flag's own
                # audit timestamp is the only clock left, and for a stream
                # flag it is the close's arrival time.
                created_at = entry.get("created_at")
                if isinstance(created_at, datetime):
                    deal_ts_ms = int(
                        created_at.replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
            if deal_ts_ms <= 0:
                continue
            flagged_windows.append(
                (
                    flag_deal_id,
                    [
                        deal_ts_ms - BROKER_CLOSE_FLAGGED_RETRY_MARGIN_MS,
                        deal_ts_ms + BROKER_CLOSE_FLAGGED_RETRY_MARGIN_MS,
                    ],
                )
            )
        retry_deal_ids = {deal_id for deal_id, _ in flagged_windows if deal_id > 0}
        booked_deal_ids: set[int] = set()
        if retry_deal_ids:
            # A deal the ledger already booked needs no retry window: its
            # flag is stale bookkeeping, and re-fetching it on every pass
            # (and every reconnect) is what used to rewrite resolution rows.
            booked_keys = await asyncio.to_thread(
                live_execution_dedupe_keys_present,
                {f"ctrader:deal:{deal_id}" for deal_id in retry_deal_ids},
            )
            booked_deal_ids = {
                int(key.rsplit(":", 1)[-1])
                for key in booked_keys
                if key.startswith("ctrader:deal:")
            }
        for deal_id, window in flagged_windows:
            if deal_id > 0 and deal_id in booked_deal_ids:
                continue
            pending.append(window)
        stored_pending = state.get("pending")
        range_pairs: list[list[int]] = []
        if isinstance(stored_pending, list):
            for item in stored_pending:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                try:
                    range_pairs.append(
                        [
                            max(int(item[0]), floor_ms),
                            min(int(item[1]), now_ms),
                        ]
                    )
                except (TypeError, ValueError):
                    continue
        for start, end in pending:
            range_pairs.append([max(int(start), floor_ms), min(int(end), now_ms)])
        clamped = self._merge_scan_ranges(range_pairs)
        # Newest coverage first: recent closes book before older backlog.
        clamped.sort(key=lambda pair: pair[1], reverse=True)
        return clamped

    async def _reconcile_broker_side_closes(
        self,
        *,
        not_before_ms: int | None = None,
        open_lots: list[dict[str, Any]] | None = None,
    ) -> int:
        """Backfill position closes the stream never delivered (#144).

        The per-signal pass above only re-reads orders this platform submitted;
        a broker-side close has no signal, so it is invisible there. The account
        deal list is the only place it shows up.

        The scan pages BACKWARDS in ``BROKER_CLOSE_RECONCILE_WINDOW_MS`` windows
        over the deal-time ranges that are not yet covered, from ``now``
        towards a floor: the oldest ledger lot still holding quantity minus
        ``BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS`` - a close can only belong to
        a position the ledger still holds - and never below ``now -
        BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS``. Coverage is tracked per
        account in ``live_adapter_state``: a pass that the request budget or
        a failure cuts short leaves the uncovered ranges pending, so the next
        pass resumes further back instead of rescanning the newest windows
        and starving the oldest ones. Newest-first ordering means recent
        closes are still covered before older ranges, and deals flagged for
        a transient cause get their windows re-queued every pass. Deal-list,
        order-list and order-details requests are capped and paced per pass;
        ``hasMore`` subdivision is depth-bounded. Anything older than the
        floor, truncated by ``hasMore``/budget, or unattributable is still
        left to the ledger-mismatch alert and operator reconciliation rather
        than booked from a stale snapshot.
        """
        if (
            self._ProtoOADealListReq is None
            or self._ProtoOADealListRes is None
            or self._ProtoOADealStatus is None
        ):
            return 0
        now_ms = int(time.time() * 1000)
        floor_ms = now_ms - BROKER_CLOSE_RECONCILE_WINDOW_MS
        order_window_from_ms: int | None = floor_ms
        # Oldest deal timestamp that could still be an open lot's close; the
        # coverage alert fires only when an uncovered range reaches it.
        exposure_from_ms = floor_ms
        if not_before_ms is not None:
            signal_floor_ms = int(not_before_ms) - BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS
            lookback_floor_ms = now_ms - BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS
            floor_ms = max(lookback_floor_ms, min(floor_ms, signal_floor_ms))
            exposure_from_ms = signal_floor_ms
            # Attribution needs the opening order even when the open lot is
            # older than the deal-list lookback cap: the order-by-position
            # window follows the lot, not the truncated scan floor.
            order_window_from_ms = max(0, signal_floor_ms)
            if signal_floor_ms < lookback_floor_ms:
                affected_lots = [
                    lot
                    for lot in (open_lots or [])
                    if (_lot_timestamp_ms(lot) or 0)
                    - BROKER_CLOSE_RECONCILE_OPEN_MARGIN_MS
                    < lookback_floor_ms
                ]
                logger.warning(
                    "cTrader broker-close reconcile floor truncated by the "
                    "maximum lookback; older closes are not reconciled this pass",
                    extra={
                        "data": {
                            "account_id": self.account_config_id,
                            "max_lookback_ms": BROKER_CLOSE_RECONCILE_MAX_LOOKBACK_MS,
                            "oldest_open_exposure_ms": int(not_before_ms),
                            "symbols": sorted(
                                {
                                    str(lot.get("symbol") or "").strip().upper()
                                    for lot in affected_lots
                                    if str(lot.get("symbol") or "").strip()
                                }
                            )[:10],
                            "signal_ids": sorted(
                                {
                                    str(lot.get("signal_id") or "")
                                    for lot in affected_lots
                                    if str(lot.get("signal_id") or "")
                                }
                            )[:10],
                        }
                    },
                )
        order_window = (
            (int(order_window_from_ms), now_ms)
            if order_window_from_ms is not None
            else None
        )
        budget: dict[str, Any] = {
            "remaining": BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS,
            "last_request_at": 0.0,
            "exhausted": False,
            "truncated_ranges": [],
            "failed_ranges": [],
        }
        pending = await self._broker_close_scan_pending_ranges(
            floor_ms=floor_ms, now_ms=now_ms
        )
        deals: list[Any] = []
        # id(deal) -> the top-level ``[from, to)`` window that returned it:
        # the fallback retry window for a deal whose own timestamp is
        # unreadable when its processing fails. ``id`` keys are safe only
        # because ``deals`` keeps every deal object alive for the whole pass.
        deal_scan_ranges: dict[int, list[int]] = {}
        remaining_pending: list[list[int]] = []
        consecutive_failures = 0
        stop_scan = False
        for pending_range in pending:
            if stop_scan:
                remaining_pending.append(list(pending_range))
                continue
            range_start = int(pending_range[0])
            range_end = int(pending_range[1])
            window_end = range_end
            while window_end > range_start:
                window_start = max(
                    range_start, window_end - BROKER_CLOSE_RECONCILE_WINDOW_MS
                )
                window_deals = await self._fetch_broker_close_deal_window(
                    window_start, window_end, budget=budget
                )
                if window_deals is None:
                    consecutive_failures += 1
                    remaining_pending.append([range_start, window_end])
                    if (
                        consecutive_failures
                        >= BROKER_CLOSE_SCAN_MAX_CONSECUTIVE_FAILURES
                        or budget["exhausted"]
                    ):
                        stop_scan = True
                    break
                consecutive_failures = 0
                deals.extend(window_deals)
                for fetched in window_deals:
                    deal_scan_ranges[id(fetched)] = [
                        int(window_start),
                        int(window_end),
                    ]
                window_end = window_start
                if budget["exhausted"]:
                    remaining_pending.append([range_start, window_end])
                    stop_scan = True
                    break
        # ``failed_ranges`` are sub-windows a failed or budget-cut fetch left
        # uncovered: they go back on the cursor with the scan leftovers.
        # ``truncated_ranges`` are ``hasMore`` ranges no rescan can page
        # (depth/width caps): alert-only, never persisted.
        uncovered_pending = self._merge_scan_ranges(
            [
                [max(int(start), floor_ms), min(int(end), now_ms)]
                for start, end in (
                    list(remaining_pending) + list(budget["failed_ranges"])
                )
            ]
        )
        truncated_ranges = [
            (int(start), int(end)) for start, end in budget["truncated_ranges"]
        ]
        requests_used = (
            BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS - int(budget["remaining"])
        )
        if truncated_ranges or budget["failed_ranges"]:
            logger.warning(
                "cTrader broker-close reconcile hasMore truncation left deal "
                "ranges uncovered this pass",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "floor_ms": floor_ms,
                        "requests_used": requests_used,
                        "max_requests": BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS,
                        "truncated_count": len(truncated_ranges),
                        "truncated_ranges": truncated_ranges[:10],
                        "failed_count": len(budget["failed_ranges"]),
                        "failed_ranges": [
                            (int(start), int(end))
                            for start, end in budget["failed_ranges"]
                        ][:10],
                    }
                },
            )
        # Process chronologically even though the scan covered windows
        # newest-first: booking order stays deterministic for partial closes.
        deals.sort(
            key=lambda deal: self._resolve_execution_timestamp_ms(
                order=None, deal=deal
            )
        )
        recovered = 0
        parent_cache: dict[int, tuple[str | None, str]] = {}
        order_cache: dict[int, Any | None] = {}
        own_order_cache: dict[int, bool] = {}
        seen_deal_ids: set[int] = set()
        try:
            flagged_deal_ids = (
                await asyncio.to_thread(
                    list_live_ledger_reconciliation_deal_ids,
                    self.account_config_id,
                )
                if deals
                else set()
            )
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "cTrader broker-close flagged-deal lookup failed; flags are "
                "not deduplicated this pass",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "error": str(exc),
                    }
                },
            )
            flagged_deal_ids = set()
        deal_retry_pending: list[list[int]] = []
        # Deals deferred because their position's close submit is still
        # marked pending: never booked, never flagged - their windows go
        # back on the cursor so a later pass retries them.
        deferred_close_deals: list[Any] = []
        try:
            for index, deal in enumerate(deals):
                # Windows abut and subdivided halves can both return a boundary
                # deal; the per-deal work (broker reads + dedupe checks) runs once.
                deal_id = _deal_id_or_none(deal)
                if deal_id is not None:
                    if deal_id in seen_deal_ids:
                        continue
                    seen_deal_ids.add(deal_id)
                try:
                    recovered += await self._reconcile_position_close_deal(
                        deal,
                        parent_cache=parent_cache,
                        order_cache=order_cache,
                        own_order_cache=own_order_cache,
                        flagged_deal_ids=flagged_deal_ids,
                        order_window=order_window,
                        pace=budget,
                        deferred_close_deals=deferred_close_deals,
                    )
                except CTraderApiError:
                    # This deal and every deal not yet reached stay
                    # unprocessed: their windows go back on the cursor so a
                    # later pass retries them instead of losing the booking.
                    deal_retry_pending.extend(
                        self._broker_close_deal_retry_ranges(
                            deals[index:],
                            deal_scan_ranges,
                            floor_ms=floor_ms,
                            now_ms=now_ms,
                        )
                    )
                    raise
                except Exception as exc:  # except-kind: money-decision
                    deal_retry_pending.extend(
                        self._broker_close_deal_retry_ranges(
                            [deal],
                            deal_scan_ranges,
                            floor_ms=floor_ms,
                            now_ms=now_ms,
                        )
                    )
                    logger.warning(
                        "cTrader broker-close reconcile skipped an unreadable deal",
                        extra={
                            "data": {
                                "account_id": self.account_config_id,
                                "deal_id": deal_id,
                                "error": str(exc),
                            }
                        },
                    )
                except BaseException:  # except-kind: money-decision
                    # ``asyncio.CancelledError`` and ``KeyboardInterrupt``
                    # bypass ``except Exception``: this deal and every deal
                    # not yet reached stay unprocessed, so their windows go
                    # back on the cursor before the interrupt propagates -
                    # the ``finally`` below must never persist a cursor that
                    # marks them covered.
                    deal_retry_pending.extend(
                        self._broker_close_deal_retry_ranges(
                            deals[index:],
                            deal_scan_ranges,
                            floor_ms=floor_ms,
                            now_ms=now_ms,
                        )
                    )
                    raise
        finally:
            # Deferred deals' windows re-join the pending set exactly like
            # failed ones: the cursor may not advance past a deal that was
            # deliberately not booked this pass.
            deal_retry_pending.extend(
                self._broker_close_deal_retry_ranges(
                    deferred_close_deals,
                    deal_scan_ranges,
                    floor_ms=floor_ms,
                    now_ms=now_ms,
                )
            )
            # The cursor advances only now - after every gathered deal was
            # processed or re-queued - so a transient read error can never
            # mark a deal's range covered without booking it.
            final_pending = self._merge_scan_ranges(
                [
                    [max(int(start), floor_ms), min(int(end), now_ms)]
                    for start, end in uncovered_pending + deal_retry_pending
                ]
            )
            await self._persist_broker_close_scan_state(
                final_pending, floor_ms=floor_ms, now_ms=now_ms
            )
        exposure_pending = [
            pair for pair in final_pending if pair[1] > exposure_from_ms
        ]
        unpageable_pending = [
            [int(start), int(end)]
            for start, end in truncated_ranges
            if int(end) > exposure_from_ms
        ]
        if final_pending:
            logger.warning(
                "cTrader broker-close reconcile left deal-time ranges "
                "uncovered; the scan cursor resumes there next pass",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "floor_ms": floor_ms,
                        "pending_count": len(final_pending),
                        "pending_ranges": final_pending[:10],
                        "requests_used": requests_used,
                        "max_requests": BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS,
                    }
                },
            )
        if (exposure_pending or unpageable_pending) and (
            not self._broker_close_coverage_alert_sent
        ):
            logger.error(
                "ALERT: cTrader broker-close reconcile has incomplete deal "
                "coverage; an open lot's close may be hidden in the "
                "uncovered ranges",
                extra={
                    "data": {
                        "alert": "broker_close_coverage_incomplete",
                        "account_id": self.account_config_id,
                        "floor_ms": floor_ms,
                        "exposure_from_ms": exposure_from_ms,
                        "pending_count": len(final_pending),
                        "pending_ranges": final_pending[:10],
                        "unpageable_count": len(unpageable_pending),
                        "requests_used": requests_used,
                        "max_requests": BROKER_CLOSE_RECONCILE_MAX_DEAL_LIST_REQUESTS,
                    }
                },
            )
            self._broker_close_coverage_alert_sent = True
        elif not exposure_pending and not unpageable_pending:
            self._broker_close_coverage_alert_sent = False
        return recovered

    def _broker_close_deal_retry_ranges(
        self,
        deal_subset: list[Any],
        deal_scan_ranges: dict[int, list[int]],
        *,
        floor_ms: int,
        now_ms: int,
    ) -> list[list[int]]:
        """``[from, to)`` windows re-queued for deals whose processing failed.

        A deal with a readable timestamp gets the same narrow window a
        transient flag retry uses; one without falls back to the scan window
        that returned it (a superset, at most one extra deal-list request),
        and finally to the whole scanned band rather than being lost.
        """
        ranges: list[list[int]] = []
        for deal in deal_subset:
            timestamp_ms = self._resolve_execution_timestamp_ms(
                order=None, deal=deal
            )
            if timestamp_ms > 0:
                ranges.append(
                    [
                        timestamp_ms - BROKER_CLOSE_FLAGGED_RETRY_MARGIN_MS,
                        timestamp_ms + BROKER_CLOSE_FLAGGED_RETRY_MARGIN_MS,
                    ]
                )
                continue
            scanned = deal_scan_ranges.get(id(deal))
            if scanned is not None:
                ranges.append(list(scanned))
            else:
                ranges.append([int(floor_ms), int(now_ms)])
        return ranges

    async def _persist_broker_close_scan_state(
        self, pending: list[list[int]], *, floor_ms: int, now_ms: int
    ) -> None:
        """Write the scan cursor off the event loop; a DB failure is loud but never fatal.

        ``set_live_adapter_state`` takes ``BEGIN IMMEDIATE`` with the shared
        30 s busy timeout: run inline it would stall every cTrader connection's
        reactor heartbeats while another writer holds the lock, so it goes
        through ``asyncio.to_thread`` like every other DB call in the pass.
        """
        try:
            await asyncio.to_thread(
                set_live_adapter_state,
                self.account_config_id,
                BROKER_CLOSE_SCAN_STATE_KEY,
                {
                    "pending": pending,
                    "floor_ms": int(floor_ms),
                    "top_ms": int(now_ms),
                },
            )
        except Exception as exc:  # except-kind: money-decision
            logger.error(
                "cTrader broker-close scan cursor could not be persisted; "
                "the next pass rescans from the previous cursor",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "error": str(exc),
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

    async def _exit_signal_uuid_for_order_id(
        self, order_id: int | None, *, position_id: int | None = None
    ) -> UUID | None:
        """The exit signal that owns this broker order id, or ``None``.

        ``ProtoOAClosePositionReq`` has no ``clientOrderId`` field (its proto
        fields are ``payloadType``, ``ctidTraderAccountId``, ``positionId``
        and ``volume``), so a close we submitted fills under an SDK-assigned
        id that parses as no signal. Attribution falls back to the broker
        order id persisted at submit time - the in-memory map first, then the
        database - but only when the resolved signal is itself an exit *as the
        ledger defines it* (``is_exit_only_signal_metadata``): a
        position-close deal carrying an *entry* order's id must still take
        the broker-close path, which resolves the position's real opener.
        ``position_id`` is the fill's own position: a resolved exit whose
        recorded position id disagrees cannot be this fill's owner.
        """
        if order_id is None:
            return None
        resolved = self._order_id_to_signal_id.get(order_id)
        if resolved is None:
            owner_ids = await asyncio.to_thread(
                find_live_signal_ids_by_broker_order_id,
                self.account_config_id,
                str(order_id),
            )
            if len(owner_ids) == 1:
                resolved = owner_ids[0]
            elif owner_ids:
                # ``broker_order_id`` is indexed, not unique: a recovered
                # duplicate can share one. Only an exit-only signal can
                # claim a position-close fill this way, so when exactly one
                # match is an exit it is the plausible owner; anything more
                # ambiguous stays unresolved and takes the broker-close
                # path, which resolves the position's real opener.
                exit_owners: list[str] = []
                for owner_id in owner_ids:
                    owner_metadata = await asyncio.to_thread(
                        get_live_signal_metadata, owner_id
                    )
                    if is_exit_only_signal_metadata(owner_metadata or {}):
                        exit_owners.append(owner_id)
                if len(exit_owners) == 1:
                    resolved = exit_owners[0]
        if not resolved:
            return None
        metadata = await asyncio.to_thread(
            get_live_signal_metadata, str(resolved)
        )
        # The LEDGER's exit predicate, not a looser one: a fill attributed to
        # a signal the ledger counts as an entry would open quantity instead
        # of reducing it.
        if not is_exit_only_signal_metadata(metadata or {}):
            return None
        if position_id is not None:
            # A broker order id is not unique, so the resolved exit can still
            # be the wrong owner: its recorded position id is the last check
            # that it closed THIS position. A signal with none recorded
            # cannot disprove the match and keeps the attribution.
            try:
                owner_position_id = self._requested_exit_position_id(metadata or {})
            except ValueError:
                # Malformed position metadata cannot disprove the match, but
                # a corrupt exit signal must not claim the fill either:
                # fail closed to the broker-close path.
                return None
            if owner_position_id is not None and owner_position_id != position_id:
                return None
        try:
            return UUID(str(resolved))
        except (ValueError, AttributeError, TypeError):
            return None

    def _close_position_fill_pending(self, *, order: Any | None, deal: Any | None) -> bool:
        """Whether this position-close fill belongs to an in-flight close submit.

        While ``submit_order`` waits on a ``ClosePositionReq`` response the
        broker order id is still unknown, so the fill's order id resolves to
        nothing and the event would be booked as a broker-side close - the
        misattribution this guard exists to prevent. The submit registers its
        position id for exactly that window; a fill on it is retained for
        reconciliation, which re-reads it once ownership is recorded. Marks
        expire so a failed submit cannot hold a genuine broker-side close on
        the position out of the stream path indefinitely.
        """
        if not self._pending_close_positions:
            return False
        if not self._event_is_position_close(order, deal):
            return False
        position_id = self._position_id_from_payload(order, deal)
        if position_id is None:
            return False
        return self._pending_close_position_active(position_id)

    def _pending_close_position_active(self, position_id: int) -> bool:
        """Whether an unexpired close submit still marks this position pending.

        Marks are per submitting signal and expire individually; expired
        entries are pruned here so a dead submit cannot hold the guard (or
        the dict) forever.
        """
        marks = self._pending_close_positions.get(position_id)
        if not marks:
            return False
        now = time.monotonic()
        for signal_id in [
            sid
            for sid, marked_at in marks.items()
            if now - marked_at > _PENDING_CLOSE_POSITION_TTL_SECONDS
        ]:
            del marks[signal_id]
            # An expired mark means a submit's broker outcome stayed unknown
            # past the TTL: retention is gone, so a later fill on this
            # position books as a broker-side close. Surface it - silent
            # expiry would hide exactly the mis-booking the mark prevents.
            logger.warning(
                "cTrader pending close mark expired before the submit's "
                "broker order id was known; fills on the position may book "
                "as broker-side closes",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "position_id": position_id,
                        "signal_id": signal_id,
                    }
                },
            )
        if not marks:
            self._pending_close_positions.pop(position_id, None)
            return False
        return True

    def _clear_pending_close_mark(self, position_id: int, signal_id: str) -> None:
        """Lift one submit's pending mark on a position.

        Only this signal's entry is removed: a second close submit still in
        flight on the same position keeps its own guard. When the position's
        last mark lifts and fills are parked waiting on it, the next
        reconciliation pass is expedited - the parked fill can attribute as
        soon as the order id is known instead of waiting out the retry
        interval.
        """
        marks = self._pending_close_positions.get(position_id)
        if marks is None:
            return
        marks.pop(signal_id, None)
        if marks:
            return
        self._pending_close_positions.pop(position_id, None)
        if self._pending_close_fill_event_ids:
            self._reconciliation_required = True
            self._reconciliation_retry_at = 0.0

    @staticmethod
    def _resolve_execution_price(*, order: Any | None, deal: Any | None) -> float:
        """The best readable execution price, or 0.0 when none is readable.

        Guarded exactly as `_resolve_filled_volume` below: finite and positive. It used to test
        truthiness alone, which admitted a negative price and - worse - NaN, because `if value:`
        is true for NaN. A NaN row makes `avg(actual_price)` NaN and makes `WHERE actual_price >
        x` silently skip it, so neither was a cosmetic problem.

        0.0 remains the "could not be read" answer rather than an error: refusing the row would
        drop `filled_qty`, and a fill the database never saw is how a duplicate live order gets
        submitted. What a reader needs is to know that the price is not evidence, and that is
        `_execution_price_presence`, stored in `actual_price_observed`.
        """
        if deal is not None:
            value = _to_float(getattr(deal, "executionPrice", 0.0))
            if math.isfinite(value) and value > 0:
                return value
        if order is not None:
            value = _to_float(getattr(order, "executionPrice", 0.0))
            if math.isfinite(value) and value > 0:
                return value
        return 0.0

    @staticmethod
    def _execution_price_presence(*, order: Any | None, deal: Any | None) -> str:
        """Whether a price was observed, sent-but-unusable, or never sent at all.

        Three states, not two, and the third is why this exists. `_resolve_execution_price`
        returns `0.0` for two different situations and a reader of the stored row cannot tell
        them apart:

            observed   a finite, positive price was read from the deal or the order
            sent_bad   the broker sent a price and it is unusable (zero, negative, non-finite)
            absent     no price was sent at all, on either payload

        `0.0` is therefore never `observed`. A price of exactly zero is `sent_bad`, which is the
        point: it looks like a price and is not one.

        The deal is checked before the order because that is the resolver's own precedence - a
        deal price is the executed one, an order price a fallback.

        Read-only and side-effect free: it logs nothing, so a caller may ask without a second
        copy of whatever `_resolve_execution_price` already said.
        """
        for payload in (deal, order):
            if payload is None:
                continue
            raw = getattr(payload, "executionPrice", None)
            if raw is None:
                continue
            # `executionPrice` is an `optional double`, so presence separates a sent zero from an
            # omitted field. A payload without `HasField` (a test double, a replay adapter) has
            # only the attribute being present at all, which is the same fallback the resolver
            # uses - the value test below still decides.
            has_field = getattr(payload, "HasField", None)
            if has_field is not None:
                try:
                    if not bool(has_field("executionPrice")):
                        continue
                except (TypeError, ValueError):
                    continue
            value = _to_float(raw)
            if math.isfinite(value) and value > 0:
                return PRICE_OBSERVED
            return PRICE_SENT_BAD
        return PRICE_ABSENT

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
    def _resolve_order_cumulative_volume(order: Any | None) -> float:
        """The order's cumulative executed volume, in broker units, or 0.0 when unreadable.

        `ProtoOAOrder.executedVolume` is the total filled on the order so far - not this
        event's slice. It is what the persistence cap compares against, and it strictly
        increases per fill, which is why it is also part of the fill's identity when the broker
        sent no per-fill id.
        """
        if order is None:
            return 0.0
        value = _to_float(getattr(order, "executedVolume", 0.0))
        if math.isfinite(value) and value > 0:
            return value
        return 0.0

    @staticmethod
    def _build_dedupe_key(
        deal: Any | None,
        *,
        order_id: int | None,
        timestamp_ms: int,
        filled_volume: float,
        order_cumulative_volume: float = 0.0,
    ) -> str:
        """A key that separates two fills without ever counting one twice.

        The broker's `dealId` is the only identifier that is unique per fill by construction,
        so it is used whenever it is present. When it is not, identity has to come from
        something else, and the previous fallback - `(order, timestamp, volume)` - collided on
        exactly the fills it existed to separate: two equal-sized fills in the same millisecond
        on one order produced one key, and the second was logged as a duplicate and dropped.
        The position then ends up short with a log line arguing against it.

        Two stronger fallbacks, in order:

        - the order's **cumulative** executed volume, which strictly increases per fill, so two
          distinct fills always differ and a repeat of the same value is a redelivery of the
          same order state;
        - a millisecond timestamp plus the volume, which is adequate but not proof.

        When none of the three is available the key cannot be derived at all, and this raises
        instead of inventing one. The neighbouring checks in `_persist_execution_event` refuse
        the same way for a non-positive volume and an unresolvable symbol; the event is then
        retained for reconciliation, which reads deals that *do* carry ids. Silently colliding
        would lose a real fill, and inventing a per-event unique key would count a redelivery
        twice - both are money errors, so neither is chosen.
        """
        deal_id = int(getattr(deal, "dealId", 0) or 0) if deal is not None else 0
        if deal_id > 0:
            return f"ctrader:deal:{deal_id}"
        if order_cumulative_volume > 0:
            cumulative_text = _format_key_quantity(order_cumulative_volume)
            return f"ctrader:order:{order_id or ''}:cum:{cumulative_text}"
        if timestamp_ms > 0:
            return (
                f"ctrader:order:{order_id or ''}:{timestamp_ms}:"
                f"{_format_key_quantity(filled_volume)}"
            )
        raise ValueError(
            "cTrader fill carries no deal id, no cumulative order volume and no timestamp, so "
            "two distinct fills on this order would be indistinguishable; retaining the event "
            "for reconciliation rather than storing it under a key that may collide."
        )

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
        self._pending_close_fill_event_ids.discard(id(event))

    def _park_pending_close_fill(self, event: Any) -> None:
        """Park a fill retained for an in-flight close submit, budget-free.

        The event waits in ``_unresolved_execution_events`` - the same deque
        every other retained fill uses - but never spends
        ``EXECUTION_RETRY_LIMIT``: the pending mark can live up to the TTL
        after an unknown-outcome submit, and a budgeted requeue would
        crash-loop the listener (and delay every other fill on the account)
        for that whole window. Reconciliation is re-armed on a paced interval
        instead: each pass may recover the close's broker order id, clear the
        mark, and requeue the event for attribution.
        """
        if len(self._unresolved_execution_events) >= UNRESOLVED_EXECUTION_EVENT_LIMIT:
            # Degenerate overflow: the deque is full, so fall back to the
            # budgeted path rather than dropping the fill.
            logger.warning(
                "cTrader pending-close fill park overflowed the unresolved "
                "deque; falling back to the budgeted requeue",
                extra={"data": {"account_id": self.ctid_trader_account_id}},
            )
            if not self._requeue_execution_event(event):
                raise RuntimeError(
                    "cTrader execution event could not be requeued after "
                    "pending-close retention; broker reconciliation is "
                    "required."
                )
            return
        self._reconciliation_required = True
        self._reconciliation_retry_at = (
            time.monotonic() + _PENDING_CLOSE_FILL_RECONCILE_SECONDS
        )
        self._unresolved_execution_events.append(event)
        self._pending_close_fill_event_ids.add(id(event))
        logger.info(
            "cTrader close fill retained while its submit is unresolved; "
            "parked for reconciliation",
            extra={
                "data": {
                    "account_id": self.ctid_trader_account_id,
                    "unresolved_events": len(self._unresolved_execution_events),
                }
            },
        )

    def _requeue_pending_close_fill(self, event: Any) -> bool:
        """Return a parked pending-close fill to the queue without spending budget."""
        try:
            self._execution_queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    def _listener_reconnect_delay_seconds(self) -> float:
        """Seconds ``listen_executions`` should still wait before reconnecting.

        A teardown (or transport loss) wakes the listener immediately so the
        gap is reconciled now rather than after the 30 s queue timeout, but a
        broker that keeps dropping the session right after auth would
        otherwise spin connect/auth/disconnect with no floor. Returns the
        remaining fraction of ``_LISTENER_RECONNECT_MIN_INTERVAL_SECONDS``.
        """
        if not self._last_session_teardown_at:
            return 0.0
        elapsed = time.monotonic() - self._last_session_teardown_at
        return max(
            0.0, _LISTENER_RECONNECT_MIN_INTERVAL_SECONDS - elapsed
        )

    async def listen_executions(self) -> None:
        backoff_seconds = 2
        while True:
            event: Any | None = None
            try:
                if not self._connected or not self._authenticated:
                    reconnect_delay = self._listener_reconnect_delay_seconds()
                    if reconnect_delay > 0:
                        await asyncio.sleep(reconnect_delay)
                    await self.connect()

                await self._run_pending_reconciliation(backoff_seconds)

                event = await asyncio.wait_for(self._execution_queue.get(), timeout=30)
                if event is _EXECUTION_RECONCILE_WAKE:
                    continue
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
            except Exception as exc:  # except-kind: money-decision
                if event is not None and isinstance(
                    exc, _PendingCloseFillRetentionError
                ):
                    self._park_pending_close_fill(event)
                    continue
                if event is not None:
                    self._pending_close_fill_event_ids.discard(id(event))
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
        except Exception as exc:  # except-kind: money-decision
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
                if id(pending_event) in self._pending_close_fill_event_ids:
                    # Parked for an in-flight close submit: requeue WITHOUT
                    # spending the retry budget - the mark can outlive the
                    # whole budget many times over.
                    requeued = self._requeue_pending_close_fill(pending_event)
                else:
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
                # Forget ids of events that left the deque: an id() is only
                # meaningful while its event object is still referenced, and
                # a stale entry would budget-free-requeue an unrelated fill.
                self._pending_close_fill_event_ids.intersection_update(
                    id(stored_event)
                    for stored_event in self._unresolved_execution_events
                )
                self._reconciliation_required = True
                self._reconciliation_retry_at = time.monotonic() + backoff_seconds
                return
            self._pending_close_fill_event_ids.clear()
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
