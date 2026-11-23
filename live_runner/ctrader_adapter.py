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
    ORDER_STATE_TERMINAL,
    ORDER_STATE_WORKING,
    BaseLiveAdapter,
    BrokerOrderView,
    ControlOutcome,
    LivePipValue,
    LiveQuote,
    coerce_positive_live_qty,
    commission_from_signed_cash_flow,
    is_unbounded_tp,
    money_digits_from_trader,
    money_scale,
    order_not_submitted,
    validate_live_quote_prices,
)
from live_runner.credential_env import resolve_accounts_env_path, resolve_env_value
from live_runner.exit_signals import ExitBelowStepError
from shared import venue_calendar
from shared.constants import DATA_DIR
from shared.database import (
    BROKER_CLOSE_REASON_CTRADER_POSITION_CLOSE,
    broker_close_signal_id,
    find_live_signal_id_by_broker_order_id,
    flag_live_ledger_reconciliation,
    get_live_signal_account_id,
    get_live_signal_broker_order_id,
    get_live_signal_metadata,
    get_live_signal_reconciliation_cursor,
    get_live_signal_timestamp,
    list_live_signal_ids_for_reconciliation,
    live_execution_dedupe_key_exists,
    record_live_broker_side_close_fill,
    save_live_execution,
    save_live_execution_up_to_cumulative,
    save_live_signal,
    set_live_signal_broker_order_id,
)
from shared.logging import get_logger
from shared.lot_grid import floor_qty_to_step
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
#: Sentinel queued on ``_execution_queue`` to wake ``listen_executions`` for a
#: pending broker reconciliation instead of letting it sleep out the 30 s
#: ``queue.get`` timeout. Never a real execution payload.
_EXECUTION_RECONCILE_WAKE = object()

#: How far back the deal-list pass looks for broker-side position closes the
#: stream never saw (#144). Bounded on purpose: an older unattributed close is
#: surfaced by the ledger-mismatch alert and periodic broker reconciliation,
#: not silently booked from a stale snapshot.
BROKER_CLOSE_RECONCILE_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000
BROKER_CLOSE_RECONCILE_MAX_ROWS = 1000


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
    sent as one tick (0 would mean "no TP").
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


def _present_proto_field(payload: Any, name: str) -> Any | None:
    """The field's value when it was sent, else ``None``.

    Spot ``bid``/``ask`` are ``optional uint64``: ``HasField`` tells an omitted
    side from a sent one. A payload without ``HasField`` (a test double) falls
    back to attribute presence.
    """
    has_field = getattr(payload, "HasField", None)
    if has_field is not None:
        try:
            if not bool(has_field(name)):
                return None
        except (TypeError, ValueError):
            return None
    return getattr(payload, name, None)


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
_CTRADER_AUTH_FAILURE_FALLBACK = "cTrader account authorization failed"


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


class CTraderLiveAdapter(BaseLiveAdapter):
    """cTrader live adapter backed by OpenApiPy."""

    broker_name = "ctrader"
    # No `supports_stop_amendment`: the daemon has no cTrader market data to
    # trail on, so this adapter stays on trailing mode "none" (issue #167; see
    # `shared.trailing.LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER`).

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
        self._rebate_logged_deal_ids: set[int] = set()
        self._unresolved_execution_events: deque[Any] = deque()
        # Deal ids already flagged ledger_reconciliation_required this process.
        # The reconcile pass re-reads the same 7-day window on every connect and
        # every pending retry; without this each unattributable deal would write
        # a new audit row and a new ERROR alert per pass, drowning the next real
        # flag in repeats. Deliberately NOT cleared on disconnect: a reconnect
        # is exactly when the pass re-runs.
        self._flagged_broker_close_deals: set[int] = set()
        self._reconciliation_required = False
        self._reconciliation_retry_at = 0.0
        self._connected = False
        self._authenticated = False
        self._connect_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
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

        self._heartbeat_payload_type: int | None = None
        self._spot_payload_type: int | None = None
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
                ProtoOADealListReq,
                ProtoOADealListRes,
                ProtoOAErrorRes,
                ProtoOAExecutionEvent,
                ProtoOAGetDynamicLeverageByIDReq,
                ProtoOAGetDynamicLeverageByIDRes,
                ProtoOAGetPositionUnrealizedPnLReq,
                ProtoOAGetPositionUnrealizedPnLRes,
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

        self._heartbeat_payload_type = ProtoHeartbeatEvent().payloadType
        self._spot_payload_type = ProtoOASpotEvent().payloadType
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
                        ) from exc
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
        sized_units = metadata.get("pip_value_units_per_qty")
        if sized_units is None and metadata.get("pip_value_source") == "caller":
            # An explicit caller pip value carries no units-per-qty, so the
            # submit-time scale check below cannot run; converting the sized
            # qty with whatever scale is current could be a different
            # position size (#146). Fail closed instead of skipping the check.
            raise ValueError(
                f"cTrader cannot verify the volume scale for {signal.symbol}: the "
                "caller-supplied pip value carries no pip_value_units_per_qty; "
                "refusing to submit a qty whose sizing scale is unknown."
            )
        if sized_units is not None:
            # #146: the daemon sized this qty with a pip value derived from the
            # symbol scale at sizing time. If the scale changed since (e.g. a
            # reconnect whose props fetch failed fell back to the static
            # table), the same qty would be a different position size.
            current_units = scale.volume_per_qty / CTRADER_VOLUME_PER_BASE_UNIT
            try:
                sized_units_value = float(sized_units)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"Invalid pip_value_units_per_qty for {signal.symbol}: {sized_units!r}"
                ) from exc
            if not math.isclose(sized_units_value, current_units, rel_tol=1e-9, abs_tol=0.0):
                raise ValueError(
                    f"cTrader volume scale for {signal.symbol} changed since sizing "
                    f"({sized_units_value!r} -> {current_units!r} units per qty); "
                    "refusing to submit a qty sized for a different contract."
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
            logger.info(
                "cTrader request written but NO RESPONSE %s; the transport is up, so this is "
                "not a credentials problem and not a TLS handshake failure. The request reached "
                "the socket and nothing came back.",
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
            except Exception:  # except-kind: loop-survival
                pass
        try:
            self._connected_event.clear()
        except Exception:  # except-kind: loop-survival
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
        # #144: broker-side position closes have no signal of ours, so the
        # per-signal pass above can never see them; the account deal list can.
        recovered += await self._reconcile_broker_side_closes()
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
        self._symbol_name_to_id.clear()
        self._order_id_to_signal_id.clear()
        self._position_ids_by_symbol.clear()
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
        # Everything up to and including the request build is provably before
        # the broker sees an order, so a failure here is marked and the caller
        # can safely retry instead of treating the outcome as unknown. The
        # `_submit_new_order_request` call below is deliberately outside.
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
                requested_position_id = self._requested_exit_position_id(
                    signal.metadata or {}
                )
                position_id = self._position_id_for_exit(
                    signal.symbol, requested_position_id
                )
                signal = signal.model_copy(
                    update={
                        "metadata": {
                            **(signal.metadata or {}),
                            "position_id": position_id,
                        }
                    }
                )
                self._resolve_order_volume(signal)

            canonical_signal_id, created = await asyncio.to_thread(
                save_live_signal, signal
            )
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
        broker_volume = round(volume_value)
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
        request.volume = self._resolve_order_volume(signal)
        request.timeInForce = self._ProtoOATimeInForce.IMMEDIATE_OR_CANCEL
        request.clientOrderId = str(signal.id)
        stop_loss = _first_metadata_float(
            metadata, ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
        )
        take_profit = _first_metadata_float(metadata, ("take_profit", "tp_price"))
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

        The broker also rejects a distance finer than the symbol's price precision
        (``Relative stop loss has invalid precision``), so each distance is floored
        to the symbol grid of ``10 ** (5 - price_digits)`` units (1 when the symbol
        is finer than 5 digits), i.e. toward the entry, so the real risk never
        exceeds the sized risk (docs/live-risk-consistency.md). A take-profit under
        one tick is sent as one tick (0 would mean "no TP"); a stop-loss under one
        tick is refused, because widening it would exceed the sized risk.
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
            # C1: explicit unbounded policy — stop-only order. cTrader treats
            # a zero relativeTakeProfit as "no take profit".
            return (distance(stop_loss, is_stop=True), 0)
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
        if isinstance(response, self._ProtoOAOrderErrorEvent):
            raise RuntimeError(
                f"Order rejected ({response.errorCode}): {response.description or 'no description'}"
            )
        return response

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
        terminal = {
            int(getattr(status_enum, name))
            for name in (
                "ORDER_STATUS_FILLED",
                "ORDER_STATUS_REJECTED",
                "ORDER_STATUS_EXPIRED",
                "ORDER_STATUS_CANCELLED",
            )
            if getattr(status_enum, name, None) is not None
        }
        if status in terminal:
            return ORDER_STATE_TERMINAL
        return None

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
        self._position_ids_by_symbol = {}
        for position in open_positions:
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
            volume = parse_money(getattr(trade_data, "volume", 0))
            qty = _broker_volume_to_qty(
                volume, self._scale_for(symbol).volume_per_qty
            )
            if int(getattr(trade_data, "tradeSide", 0) or 0) == self._ProtoOATradeSide.SELL:
                qty = -qty
            normalized_symbol = str(symbol).strip().upper()
            row: dict[str, Any] = {
                "symbol": normalized_symbol,
                "qty": qty,
                "entry_price": parse_money(getattr(position, "price", 0)),
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

    async def _fetch_position_orders(self, position_id: int) -> Any | None:
        """Every order the broker ties to one position, opening and closing."""
        if (
            self._ProtoOAOrderListByPositionIdReq is None
            or self._ProtoOAOrderListByPositionIdRes is None
        ):
            return None
        request = self._ProtoOAOrderListByPositionIdReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.positionId = int(position_id)
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
        if metadata and (
            str(metadata.get("signal_intent") or "") == "exit"
            or bool(metadata.get("exit_only"))
            or str(metadata.get("risk_sizing_mode") or "")
            == EXIT_ONLY_RISK_SIZING_MODE
        ):
            return None
        return candidate

    async def _resolve_position_parent_signal(
        self, position_id: int
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
        orders = await self._fetch_position_orders(position_id)
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
        self, symbol: str, cause: str, detail: Mapping[str, Any]
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
        is flagged every time.
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
        }
        if position_id is None:
            self._flag_ledger_reconciliation(symbol, "no_position_id", base_detail)
            return False
        parent_signal_id, cause = await self._resolve_position_parent_signal(position_id)
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

    async def _fetch_order_details(self, order_id: int) -> Any | None:
        """One order's details for the reconcile pass's cumulative cap."""
        if (
            self._ProtoOAOrderDetailsReq is None
            or self._ProtoOAOrderDetailsRes is None
        ):
            return None
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
    ) -> int:
        """Book one deal-list close the stream missed, or flag it (#144)."""
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
        order_id = self._positive_int(getattr(deal, "orderId", 0))
        if order_id is not None:
            # A deal our own order made is the per-signal reconcile's job, not
            # this pass's: booking it here too would close the ledger twice.
            if order_id not in own_order_cache:
                own_order_cache[order_id] = bool(
                    self._order_id_to_signal_id.get(order_id)
                ) or bool(
                    await asyncio.to_thread(
                        find_live_signal_id_by_broker_order_id,
                        self.account_config_id,
                        str(order_id),
                    )
                )
            if own_order_cache[order_id]:
                return 0
        if position_id not in parent_cache:
            parent_cache[position_id] = await self._resolve_position_parent_signal(
                position_id
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
                    "source": "reconcile",
                },
            )
            return 0
        order = None
        if order_id is not None:
            if order_id not in order_cache:
                order_cache[order_id] = await self._fetch_order_details(order_id)
            order = order_cache[order_id]
        dedupe_key = self._broker_close_dedupe_key(deal)
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
                    "parent_signal_id": parent_signal_id,
                    "source": "reconcile",
                },
            )
            return 0
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

    async def _reconcile_broker_side_closes(self) -> int:
        """Backfill position closes the stream never delivered (#144).

        The per-signal pass above only re-reads orders this platform submitted;
        a broker-side close has no signal, so it is invisible there. The account
        deal list is the only place it shows up. Bounded to a recent window and
        a row cap: anything older or unattributable is flagged for the periodic
        broker reconciliation rather than booked from stale state.
        """
        if (
            self._ProtoOADealListReq is None
            or self._ProtoOADealListRes is None
            or self._ProtoOADealStatus is None
        ):
            return 0
        now_ms = int(time.time() * 1000)
        request = self._ProtoOADealListReq()
        request.ctidTraderAccountId = self.ctid_trader_account_id
        request.fromTimestamp = now_ms - BROKER_CLOSE_RECONCILE_LOOKBACK_MS
        request.toTimestamp = now_ms
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
                        "error": str(exc),
                    }
                },
            )
            return 0
        if not isinstance(response, self._ProtoOADealListRes):
            logger.warning(
                "Unexpected deal-list response during broker-close reconcile",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "response_type": type(response).__name__,
                    }
                },
            )
            return 0
        if getattr(response, "hasMore", False):
            # maxRows truncated the window: older closes in it are silently
            # missed, so the truncation has to be loud rather than discovered
            # as a ledger gap weeks later.
            logger.warning(
                "cTrader deal list reports hasMore during broker-close reconcile; "
                "closes older than the returned rows are not reconciled this pass",
                extra={
                    "data": {
                        "account_id": self.account_config_id,
                        "max_rows": BROKER_CLOSE_RECONCILE_MAX_ROWS,
                    }
                },
            )
        recovered = 0
        parent_cache: dict[int, tuple[str | None, str]] = {}
        order_cache: dict[int, Any | None] = {}
        own_order_cache: dict[int, bool] = {}
        for deal in getattr(response, "deal", []) or []:
            try:
                recovered += await self._reconcile_position_close_deal(
                    deal,
                    parent_cache=parent_cache,
                    order_cache=order_cache,
                    own_order_cache=own_order_cache,
                )
            except CTraderApiError:
                raise
            except Exception as exc:  # except-kind: money-decision
                logger.warning(
                    "cTrader broker-close reconcile skipped an unreadable deal",
                    extra={
                        "data": {
                            "account_id": self.account_config_id,
                            "deal_id": _deal_id_or_none(deal),
                            "error": str(exc),
                        }
                    },
                )
        return recovered

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

    async def listen_executions(self) -> None:
        backoff_seconds = 2
        while True:
            event: Any | None = None
            try:
                if not self._connected or not self._authenticated:
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
