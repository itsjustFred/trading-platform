from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import pandas as pd
from alpaca.trading.enums import OrderClass
from alpaca.trading.requests import StopLossRequest, TakeProfitRequest

from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    BaseLiveAdapter,
    ControlOutcome,
    coerce_positive_live_qty,
    is_unbounded_tp,
    order_not_submitted,
)
from live_runner.credential_env import parse_bool_env, resolve_env_value
from shared.database import (
    get_live_signal_broker_order_id,
    get_live_signal_filled_qty,
    get_live_signal_reconciliation_cursor,
    list_live_signal_ids_for_reconciliation,
    save_live_execution,
    save_live_execution_up_to_cumulative,
    save_live_signal,
    set_live_signal_broker_order_id,
)
from shared.logging import get_logger
from shared.models import LiveExecution, LiveSignal

logger = get_logger("live-runner")

_ALPACA_ACC_RE = re.compile(r"^ALPACA_API_KEY_ACC(?P<index>\d+)$")

_SL_METADATA_KEYS = ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
_TP_METADATA_KEYS = ("take_profit", "tp_price")


def _normalize_broker_order_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"none", "null", "false"}:
        return None
    try:
        uuid_value = UUID(normalized)
    except (AttributeError, TypeError, ValueError):
        return None
    if uuid_value.int == 0:
        return None
    return str(uuid_value)


def _control_order_id(value: Any) -> str | None:
    """An order id usable for a control call, without assuming a UUID shape.

    `_normalize_broker_order_id` is deliberately strict because it guards the
    submission path against junk. Control calls only need to name an order the
    broker just returned, and a fake client (or a broker that changes its id
    format) must not be reported as "unidentifiable" for that reason.
    """
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"none", "null", "false"}:
        return None
    return normalized


def _order_attr(order: Any, name: str, default: Any = None) -> Any:
    if isinstance(order, Mapping):
        return order.get(name, default)
    return getattr(order, name, default)


def _order_legs(order: Any) -> list[Any]:
    legs = _order_attr(order, "legs", None)
    return list(legs) if isinstance(legs, (list, tuple)) else []


def _order_symbol(order: Any) -> str:
    return str(_order_attr(order, "symbol", "") or "").strip().upper()


def _order_side(order: Any) -> str:
    """The order's side as ``buy``/``sell``, whether it arrives as an enum or a string."""
    raw = _order_attr(order, "side", "")
    text = str(getattr(raw, "value", raw) or "").strip().lower()
    return text.rsplit(".", 1)[-1]


def _alpaca_timeframe(value: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    normalized = str(value or "").strip().lower()
    units = {
        "1m": (1, TimeFrameUnit.Minute),
        "5m": (5, TimeFrameUnit.Minute),
        "15m": (15, TimeFrameUnit.Minute),
        "30m": (30, TimeFrameUnit.Minute),
        "1h": (1, TimeFrameUnit.Hour),
        "1d": (1, TimeFrameUnit.Day),
    }
    if normalized not in units:
        raise ValueError(f"Unsupported Alpaca market-data timeframe: {value!r}")
    amount, unit = units[normalized]
    return TimeFrame(amount=amount, unit=unit), normalized


# Markets are closed for most of the calendar. A US equity session is 6.5 hours
# on five days a week, so one calendar day buys ~27% of a trading day and a week
# buys ~5/7 of the sessions. Sizing an intraday lookback by the bare bar duration
# therefore under-requests by ~5.2x: 200 30-minute bars need ~6000 trading
# minutes, which is ~21 calendar days, not the 220 hours (9 days) a `*2.2`
# multiplier buys. The old value returned a short history, and the `tail()` then
# shipped it - silently changing every lookback-dependent indicator relative to
# the backtest. 6.5 covers the session fraction, weekends and a holiday week;
# the widening retry below covers sparse IEX data on top of it.
_CALENDAR_TO_TRADING_SLACK = 6.5
_DAILY_CALENDAR_TO_TRADING_SLACK = 1.6


def _alpaca_market_data_start(
    timeframe: str,
    limit: int,
    *,
    slack_multiplier: float = 1.0,
) -> datetime:
    normalized = str(timeframe or "").strip().lower()
    bars = max(int(limit), 1) * max(float(slack_multiplier), 1.0)
    if normalized.endswith("m"):
        # The bar duration is part of the request: `limit` bars of 30 minutes is
        # 6000 trading minutes, not 200. Omitting it was the other half of the
        # short-history bug, on top of the calendar/trading slack.
        try:
            bar_minutes = float(normalized[:-1])
        except ValueError:
            bar_minutes = 1.0
        return datetime.now(timezone.utc) - timedelta(
            minutes=bars * bar_minutes * _CALENDAR_TO_TRADING_SLACK
        )
    if normalized.endswith("h"):
        try:
            bar_hours = float(normalized[:-1])
        except ValueError:
            bar_hours = 1.0
        return datetime.now(timezone.utc) - timedelta(
            hours=bars * bar_hours * _CALENDAR_TO_TRADING_SLACK
        )
    return datetime.now(timezone.utc) - timedelta(
        days=bars * _DAILY_CALENDAR_TO_TRADING_SLACK
    )


def _parse_bool_env(name: str, value: str | None, *, default: bool = True) -> bool:
    """Strict boolean parsing shared with live_runner.credential_env.

    A typo'd value raises instead of silently selecting live/paper mode.
    """
    return parse_bool_env(name, value, default=default)


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_parseable_qty(value: Any) -> bool:
    """Whether ``value`` yields a finite float quantity."""
    try:
        return math.isfinite(float(Decimal(str(value))))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _position_qty(position: Mapping[str, Any]) -> float:
    """The broker position's signed quantity, refusing to guess on a bad value.

    ``_to_float(..., default=0.0)`` maps an *unparseable* ``qty`` onto "flat",
    which would let a position with a malformed quantity be silently skipped as
    "no position" — the flatten would then report success and a STOPPED
    deployment would be stamped enforced while real exposure stayed open.

    :func:`get_positions` marks such a row with ``qty_unparseable`` (the
    displayed ``qty`` is still the coerced ``0.0`` so ordinary consumers keep
    working). This helper is the risk-path reader: it raises on that marker, and
    on a non-finite quantity, rather than returning a misleading zero.
    """
    if position.get("qty_unparseable"):
        raise ValueError(
            f"broker position for {position.get('symbol')!r} has an unparseable "
            f"qty ({position.get('qty_raw')!r}); refusing to treat it as flat"
        )
    if "qty" not in position:
        return 0.0
    value = position.get("qty")
    if value is None:
        return 0.0
    try:
        qty = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"broker position for {position.get('symbol')!r} has an unparseable "
            f"qty ({value!r}); refusing to treat it as flat"
        ) from exc
    if not math.isfinite(qty):
        raise ValueError(
            f"broker position for {position.get('symbol')!r} has a non-finite "
            f"qty ({value!r}); refusing to treat it as flat"
        )
    return qty


def _is_flat(qty: float) -> bool:
    """Whether a broker quantity means "no position".

    Comparing a quantity to ``0.0`` with ``==`` is a float-equality bug (Sonar
    S1244): a position left with a sub-unit residue would read as open forever,
    and a quantity a hair above zero would never be treated as closed. The
    tolerance is absolute and far below any tradable unit (brokers quote to at
    most 1e-9), so a real position cannot be mistaken for flat.

    Non-finite values are not flat - ``_position_qty`` already refuses them, and
    a NaN that slipped through must not read as "nothing open".
    """
    return math.isclose(qty, 0.0, rel_tol=0.0, abs_tol=1e-12)


def _resolve_optional_float(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
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


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _default_alpaca_credentials(values: Mapping[str, str]) -> tuple[str, str, bool] | None:
    api_key = str(values.get("ALPACA_API_KEY", "")).strip()
    secret_key = str(values.get("ALPACA_SECRET_KEY", "")).strip()
    if api_key and secret_key:
        raw_paper = values.get("ALPACA_PAPER")
        paper = _parse_bool_env(
            "ALPACA_PAPER",
            str(raw_paper).strip() if raw_paper is not None else None,
            default=True,
        )
        return api_key, secret_key, paper
    return None


def _alpaca_acc_indexes(values: Mapping[str, str]) -> list[int]:
    return sorted(
        {
            int(match.group("index"))
            for key in values
            if (match := _ALPACA_ACC_RE.match(str(key)))
        }
    )


def _alpaca_credential_block(
    values: Mapping[str, str],
    idx: int,
) -> dict[str, str | bool] | None:
    """Parse one ACC{idx} credential block.

    Returns None when the block has no complete key/secret pair; raises
    ValueError when its paper flag fails the strict parse.
    """
    api_key = str(values.get(f"ALPACA_API_KEY_ACC{idx}", "")).strip()
    secret_key = str(values.get(f"ALPACA_SECRET_KEY_ACC{idx}", "")).strip()
    if not api_key or not secret_key:
        return None

    endpoint = str(values.get(f"ALPACA_ENDPOINT_ACC{idx}", "")).strip().lower()
    if endpoint:
        paper = "paper" in endpoint
    else:
        raw_paper = values.get(f"ALPACA_PAPER_ACC{idx}")
        paper = _parse_bool_env(
            f"ALPACA_PAPER_ACC{idx}",
            str(raw_paper).strip() if raw_paper is not None else None,
            default=True,
        )
    return {
        "source_index": f"ACC{idx}",
        "api_key": api_key,
        "secret_key": secret_key,
        "paper": paper,
        "account_id_hint": str(values.get(f"ALPACA_ACCOUNT_ID_ACC{idx}", "")).strip(),
        "login_hint": (
            str(values.get(f"ALPACA_BROKER_LOGIN_ACC{idx}", "")).strip()
            or str(values.get(f"ALPACA_ACCOUNT_NUMBER_ACC{idx}", "")).strip()
        ),
    }


def _alpaca_credential_blocks(
    values: Mapping[str, str],
    indexes: list[int],
) -> list[dict[str, str | bool]]:
    blocks: list[dict[str, str | bool]] = []
    for idx in indexes:
        block = _alpaca_credential_block(values, idx)
        if block is not None:
            blocks.append(block)
    return blocks


def _alpaca_block_credentials(block: dict[str, str | bool]) -> tuple[str, str, bool]:
    return (
        str(block["api_key"]),
        str(block["secret_key"]),
        bool(block["paper"]),
    )


def _matching_alpaca_block(
    blocks: list[dict[str, str | bool]],
    normalized_account_id: str,
    normalized_login: str,
) -> tuple[str, str, bool] | None:
    for block in blocks:
        account_id_hint = str(block.get("account_id_hint", "")).strip().lower()
        if account_id_hint and account_id_hint == normalized_account_id:
            return _alpaca_block_credentials(block)
        login_hint = str(block.get("login_hint", "")).strip().lower()
        if login_hint and normalized_login and login_hint == normalized_login:
            return _alpaca_block_credentials(block)
    return None


def _alpaca_blocks_have_hints(blocks: list[dict[str, str | bool]]) -> bool:
    return any(
        str(block.get("account_id_hint", "")).strip() or str(block.get("login_hint", "")).strip()
        for block in blocks
    )


def resolve_alpaca_credentials(
    *,
    account_id: str = "",
    broker_login: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, bool] | None:
    """Resolve Alpaca credentials from env (default or ACC{N} blocks)."""
    values = dict(env) if env is not None else dict(os.environ)

    indexes = _alpaca_acc_indexes(values)
    blocks: list[dict[str, str | bool]] = []
    if indexes:
        blocks = _alpaca_credential_blocks(values, indexes)

    normalized_account_id = str(account_id or "").strip().lower()
    normalized_login = str(broker_login or "").strip().lower()
    if blocks:
        matched = _matching_alpaca_block(blocks, normalized_account_id, normalized_login)
        if matched is not None:
            return matched
        if (normalized_account_id or normalized_login) and _alpaca_blocks_have_hints(
            blocks
        ):
            logger.warning(
                "No exact Alpaca credential block match; refusing ambiguous fallback",
                extra={
                    "data": {
                        "account_id": account_id,
                        "broker_login": broker_login,
                    }
                },
            )
            return None

    # Default pair applies only when the mapping is still unique: the legacy
    # single-account setup (no ACC{N} blocks, or exactly one block that the
    # account did not identify itself against). With two or more credential
    # blocks there is no unique mapping, so an unmatched account must never
    # silently route through another account's keys.
    if len(blocks) > 1:
        logger.warning(
            "No unique Alpaca credential block mapping; refusing ambiguous fallback",
            extra={
                "data": {
                    "account_id": account_id,
                    "broker_login": broker_login,
                    "candidate_blocks": len(blocks),
                    "hints_present": _alpaca_blocks_have_hints(blocks),
                }
            },
        )
        return None

    default_credentials = _default_alpaca_credentials(values)
    if default_credentials is not None:
        return default_credentials

    if not blocks:
        return None
    # Single remaining block with an unidentified account: the legacy
    # single-account setup. Keep it working, but make it visible — this is
    # the one path where an account can trade through keys it did not map to.
    logger.warning(
        "Unidentified account using the only configured Alpaca credential block",
        extra={
            "data": {
                "account_id": account_id,
                "broker_login": broker_login,
                "block_source_index": str(blocks[0].get("source_index", "")),
            }
        },
    )
    return _alpaca_block_credentials(blocks[0])


class AlpacaLiveAdapter(BaseLiveAdapter):
    """Live adapter for Alpaca trading + trade update streaming."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool | None = None,
        account_id: str = "",
        env_values: Mapping[str, str] | None = None,
    ):
        # C1: file-first credential precedence — explicit args win, then
        # .accounts.env via credential_env, then os.environ for ABSENT keys.
        # A present-but-empty file value stays empty (never shadowed).
        self.api_key = api_key or resolve_env_value(env_values, "ALPACA_API_KEY")
        self.secret_key = secret_key or resolve_env_value(env_values, "ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            logger.warning(
                "Alpaca adapter falling back to resolved-env credentials",
                extra={"data": {"account_id": account_id}},
            )
        self.paper = (
            _parse_bool_env(
                "ALPACA_PAPER",
                resolve_env_value(env_values, "ALPACA_PAPER") or None,
                default=True,
            )
            if paper is None
            else paper
        )
        self.account_id = account_id
        self._trading_client: Any | None = None
        self._trading_stream: Any | None = None
        self._historical_data_client: Any | None = None
        # #78: subscribe exactly once per stream instance. Re-subscribing
        # the same (or a reconnected) stream duplicates callbacks. The
        # subscribed stream object itself is tracked (not its id(): CPython
        # reuses addresses after GC, so a stale id could skip subscribing a
        # NEW stream — a silent mute after disconnect/reconnect).
        self._subscribed_stream: Any | None = None
        self._connected = False

        if not self.api_key or not self.secret_key:
            raise ValueError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )

    async def connect(self) -> None:
        """Initialize REST and websocket clients, and verify account access."""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
            from alpaca.trading.stream import TradingStream
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        self._trading_client = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        self._trading_stream = TradingStream(self.api_key, self.secret_key, paper=self.paper)
        self._historical_data_client = StockHistoricalDataClient(
            self.api_key, self.secret_key
        )
        try:
            self._trading_client.get_account()
        except Exception:
            await self.disconnect()
            raise
        self._connected = True
        logger.info(
            "Connected Alpaca live adapter",
            extra={"data": {"account_id": self.account_id, "paper": self.paper}},
        )
        try:
            await self.reconcile_executions()
        except Exception as exc:
            logger.warning(
                "Alpaca execution reconciliation failed; fills may be missing until retry",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )

    async def _fetch_reconciliation_page(
        self,
        before: tuple[datetime, str] | None,
        max_signals: int,
    ) -> list[str]:
        if before is None:
            return await asyncio.to_thread(
                list_live_signal_ids_for_reconciliation,
                self.account_id,
                max_signals,
            )
        return await asyncio.to_thread(
            list_live_signal_ids_for_reconciliation,
            self.account_id,
            max_signals,
            before=before,
        )

    async def _reconcile_signal_fill(self, signal_id: str) -> bool:
        try:
            order = await asyncio.to_thread(
                self._trading_client.get_order_by_client_id, signal_id  # type: ignore[union-attr]  # Optional client/outcome is guarded by the caller; mypy cannot follow it
            )
        except Exception:
            # Unknown to this account or not found.
            return False
        broker_filled_qty = _to_float(
            getattr(order, "filled_qty", None), default=0.0
        )
        local_filled_qty = await asyncio.to_thread(
            get_live_signal_filled_qty, signal_id
        )
        missing_filled_qty = broker_filled_qty - local_filled_qty
        if missing_filled_qty <= 0:
            return False
        broker_order_id = _normalize_broker_order_id(getattr(order, "id", None))
        if broker_order_id is None:
            return False
        execution = LiveExecution(
            signal_id=UUID(signal_id),
            actual_price=_to_float(
                getattr(order, "filled_avg_price", None), default=0.0
            ),
            filled_qty=missing_filled_qty,
            commission=0.0,
            timestamp=_to_datetime(getattr(order, "filled_at", None)),
            broker_order_id=broker_order_id,
            dedupe_key=(
                f"alpaca:order:{broker_order_id}:reconciled:{broker_filled_qty}"
            ),
        )
        persisted_qty = await asyncio.to_thread(
            save_live_execution_up_to_cumulative, execution, broker_filled_qty
        )
        if persisted_qty <= 0.0:
            return False
        logger.info(
            "Reconciled missed Alpaca fill",
            extra={
                "data": {
                    "signal_id": signal_id,
                    "broker_order_id": broker_order_id,
                    "filled_qty": persisted_qty,
                    "broker_cumulative_filled_qty": broker_filled_qty,
                    "local_cumulative_filled_qty": local_filled_qty,
                }
            },
        )
        return True

    async def reconcile_executions(self, *, max_signals: int = 200) -> int:
        """Backfill missing fill quantity for recent signals in this Alpaca account.

        Recovers fills that completed while the daemon was down or crashed between
        broker submit and stream-event persistence. Uses the client_order_id == signal.id
        convention. Broker order quantities are cumulative, while local executions are
        stored as event deltas, so only the positive cumulative difference is persisted.
        """
        if self._trading_client is None:
            return 0
        recovered = 0
        before: tuple[datetime, str] | None = None
        while True:
            signal_ids = await self._fetch_reconciliation_page(before, max_signals)
            if not signal_ids:
                break
            for signal_id in signal_ids:
                if await self._reconcile_signal_fill(signal_id):
                    recovered += 1
            if len(signal_ids) < max_signals:
                break
            next_before = await asyncio.to_thread(
                get_live_signal_reconciliation_cursor, signal_ids[-1]
            )
            if next_before is None or next_before == before:
                break
            before = next_before
        return recovered

    async def disconnect(self) -> None:
        stream = self._trading_stream
        self._trading_stream = None
        self._subscribed_stream = None
        self._trading_client = None
        self._historical_data_client = None
        self._connected = False
        if stream is None:
            return

        stop_ws = getattr(stream, "stop_ws", None)
        stop = getattr(stream, "stop", None)
        try:
            if callable(stop_ws):
                result = stop_ws()
                if inspect.isawaitable(result):
                    await result
            elif callable(stop):
                result = stop()
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            logger.warning(
                "Failed to stop Alpaca stream cleanly",
                extra={"data": {"account_id": self.account_id, "error": str(exc)}},
            )

    def _load_alpaca_order_types(
        self,
    ) -> tuple[Any, Any, Any]:
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        return OrderSide, TimeInForce, MarketOrderRequest

    async def _recover_existing_signal_order(
        self,
        trading_client: Any,
        canonical_signal_id: str,
    ) -> str:
        raw_existing_order_id = await asyncio.to_thread(
            get_live_signal_broker_order_id, canonical_signal_id
        )
        if raw_existing_order_id:
            existing_order_id = _normalize_broker_order_id(raw_existing_order_id)
            if existing_order_id is None:
                raise RuntimeError(
                    "Alpaca persisted broker order id is invalid; refusing duplicate submission."
                )
            return existing_order_id

        try:
            recovered_order = await asyncio.to_thread(
                trading_client.get_order_by_client_id,
                canonical_signal_id,
            )
        except Exception as exc:
            raise RuntimeError(
                "Alpaca canonical signal already exists and broker recovery failed; "
                "refusing possible duplicate submission."
            ) from exc
        recovered_order_id = _normalize_broker_order_id(getattr(recovered_order, "id", None))
        if recovered_order_id is None:
            raise RuntimeError(
                "Alpaca canonical signal already exists without a recoverable broker "
                "order id; refusing possible duplicate submission."
            )
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id,
            canonical_signal_id,
            recovered_order_id,
        )
        if not persisted:
            raise RuntimeError("Failed to persist recovered Alpaca broker order id.")
        logger.info(
            "Recovered accepted Alpaca order without resubmitting",
            extra={
                "data": {
                    "signal_id": canonical_signal_id,
                    "broker_order_id": recovered_order_id,
                }
            },
        )
        return recovered_order_id

    def _resolve_broker_sl_tp(self, signal: LiveSignal) -> tuple[float, float]:
        """Resolve absolute SL/TP prices from signal metadata.

        Fail closed: sizing divided the risk budget by the stop distance, so an
        order without SL/TP would open an unbounded position. Exit-only orders
        (risk_sizing_mode == 'exit_only') close an existing position, reduce
        exposure and legitimately carry no SL/TP — the guard applies to
        opening orders only.
        """
        metadata = signal.metadata or {}
        stop_loss = _resolve_optional_float(metadata, _SL_METADATA_KEYS)
        take_profit = _resolve_optional_float(metadata, _TP_METADATA_KEYS)
        exit_only = (
            str(metadata.get("risk_sizing_mode") or "") == EXIT_ONLY_RISK_SIZING_MODE
        )
        # C1: explicit unbounded-take-profit policy (processor-declared) —
        # stop-only order, never an invented bracket.
        unbounded_tp = not exit_only and take_profit is None and is_unbounded_tp(metadata)
        missing = [
            name
            for name, value in (("stop_loss", stop_loss), ("take_profit", take_profit))
            if value is None
        ]
        if missing and not exit_only and not (unbounded_tp and missing == ["take_profit"]):
            raise ValueError(
                "Refusing to submit Alpaca order without "
                f"{' and '.join(missing)} (signal_id={signal.id}, "
                f"symbol={signal.symbol}); opening an unbounded position is not allowed."
            )
        if missing:
            if unbounded_tp and stop_loss is not None:
                # CodeRabbit round: the early return must not bypass the
                # finite/positive validation below — an invalid stop would
                # reach StopLossRequest unchecked.
                if math.isnan(stop_loss) or math.isinf(stop_loss) or stop_loss <= 0:
                    raise ValueError(
                        f"Invalid stop_loss price for {signal.symbol}: {stop_loss!r}; "
                        "refusing to submit."
                    )
                return float(stop_loss), float("nan")
            return float("nan"), float("nan")
        for name, value in (("stop_loss", stop_loss), ("take_profit", take_profit)):
            if math.isnan(value) or math.isinf(value) or value <= 0:  # type: ignore[arg-type,operator]  # value validated at runtime / narrowed by an earlier guard
                raise ValueError(
                    f"Invalid {name} price for {signal.symbol}: {value!r}; "
                    "refusing to submit."
                )
        return float(stop_loss), float(take_profit)  # type: ignore[arg-type]  # value validated at runtime / narrowed by an earlier guard

    def _build_order_request(
        self,
        signal: LiveSignal,
        *,
        qty: float,
        order_side: Any,
        time_in_force: Any,
        market_request_cls: Any,
        client_order_id: str,
    ) -> Any:
        qty = coerce_positive_live_qty(qty, field_name="Alpaca order qty")
        if signal.action not in {"buy", "sell"}:
            # Fail closed: an unmapped action must never fall through to SELL.
            raise ValueError(f"Unsupported signal action: {signal.action}")
        stop_loss_price, take_profit_price = self._resolve_broker_sl_tp(signal)
        extended_hours = bool((signal.metadata or {}).get("extended_hours"))
        if signal.action == "buy":
            side = order_side.BUY
        else:
            side = order_side.SELL
        exit_only = (
            str((signal.metadata or {}).get("risk_sizing_mode") or "")
            == EXIT_ONLY_RISK_SIZING_MODE
        )
        if extended_hours and not exit_only:
            # The broker rejects bracket orders with extended_hours enabled
            # ("bracket order has non-zero extended_hours value", HTTP 422), and
            # OTO bracket legs cannot execute outside regular hours anyway.
            # Fail closed rather than silently dropping the SL/TP the risk
            # sizing assumed: the caller must choose bounded-hours bracket or
            # naked extended-hours — and naked entries are not allowed.
            raise ValueError(
                "Extended-hours entry with mandatory SL/TP is not supported: "
                "the broker rejects bracket orders with extended_hours enabled "
                f"(signal_id={signal.id}, symbol={signal.symbol})."
            )
        # C1: explicit unbounded policy rides as OTO (entry + stop leg,
        # no take-profit leg) instead of an invented bracket.
        unbounded_tp = (
            not exit_only
            and isinstance(take_profit_price, float)
            and math.isnan(take_profit_price)
            and is_unbounded_tp(signal.metadata)
        )
        if unbounded_tp:
            order_class = OrderClass.OTO
            take_profit_leg = None
        else:
            order_class = OrderClass.SIMPLE if exit_only else OrderClass.BRACKET
            take_profit_leg = (
                None if exit_only else TakeProfitRequest(limit_price=take_profit_price)
            )
        return market_request_cls(
            time_in_force=time_in_force.DAY,
            extended_hours=False,
            order_class=order_class,
            take_profit=take_profit_leg,
            stop_loss=None if exit_only else StopLossRequest(stop_price=stop_loss_price),
            symbol=signal.symbol,
            qty=qty,
            side=side,
            client_order_id=client_order_id,
        )

    async def _persist_submitted_order_id(
        self,
        canonical_signal_id: str,
        broker_order_id: str,
    ) -> None:
        normalized_order_id = _normalize_broker_order_id(broker_order_id)
        if normalized_order_id is None:
            raise RuntimeError("Alpaca order response has no valid broker order id.")
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id, canonical_signal_id, normalized_order_id
        )
        if not persisted:
            raise RuntimeError("Failed to persist Alpaca broker order id.")

    async def _require_exit_position(self, signal: LiveSignal, qty: float) -> float:
        """Fail closed unless an exit-only order provably reduces a live position.

        Alpaca closes a position with an ordinary opposing market order, so an
        exit-only order whose position has already gone would silently OPEN one -
        and because the exit-only path drops the mandatory SL/TP legs it would
        open it unbounded. The same is true of an order larger than the position:
        the excess becomes a new naked position on the other side.

        Returns the quantity to submit, capped at the broker's current position.
        Capping rather than refusing is deliberate: the order's intent is to
        close what exists, and refusing would leave the exposure in place. The
        cap is logged because a size mismatch means the daemon's book moved
        under us.
        """
        if not (
            str((signal.metadata or {}).get("risk_sizing_mode") or "")
            == EXIT_ONLY_RISK_SIZING_MODE
        ):
            return qty
        symbol = str(signal.symbol or "").strip().upper()
        quantities = [
            quantity
            for position in await self.get_positions()
            if str(position.get("symbol") or "").strip().upper() == symbol
            and not _is_flat(quantity := _position_qty(position))
        ]
        if not quantities:
            raise ValueError(
                "Refusing Alpaca exit-only order for "
                f"{symbol} (action={signal.action}, signal_id={signal.id}): "
                "no live broker position to close. An exit-only order without a "
                "position would OPEN one, and without its mandatory SL/TP."
            )
        if any(quantity > 0 for quantity in quantities) and any(
            quantity < 0 for quantity in quantities
        ):
            raise ValueError(
                f"Refusing Alpaca exit-only order for {symbol}: the broker book "
                "holds mixed long and short positions, so no single close is "
                "unambiguous."
            )
        close_action = "sell" if quantities[0] > 0 else "buy"
        if signal.action != close_action:
            position_side = "long" if close_action == "sell" else "short"
            raise ValueError(
                f"Refusing Alpaca exit-only order for {symbol}: a {signal.action} "
                f"does not close a {position_side} position (its close side is "
                f"{close_action!r}); it would open or extend exposure instead of "
                "reducing it."
            )
        position_qty = abs(quantities[0])
        if qty > position_qty:
            logger.warning(
                "Capping Alpaca exit-only order to the broker position; the "
                "requested quantity exceeded it and the excess would open a new "
                "naked position",
                extra={
                    "data": {
                        "signal_id": str(signal.id),
                        "symbol": symbol,
                        "requested_qty": qty,
                        "broker_position_qty": position_qty,
                    }
                },
            )
            return position_qty
        return qty

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        # Everything up to and including the request build is provably before
        # the broker sees an order, so a failure here is marked and the caller
        # can safely retry instead of treating the outcome as unknown. The
        # `trading_client.submit_order` call below is deliberately outside.
        with order_not_submitted():
            if not self._connected or self._trading_client is None:
                await self.connect()
            trading_client = self._trading_client
            if trading_client is None:
                raise RuntimeError("Alpaca trading client is unavailable after connect.")

            # B-8: validate the cheap in-memory facts BEFORE the first persistence.
            # A signal row persisted without a broker order wedges every redelivery
            # on the duplicate-persisted recovery path.
            if signal.action not in {"buy", "sell"}:
                raise ValueError(f"Unsupported action '{signal.action}'")

            # C1/B2: size exactly once upstream (deployment RiskConfig); the
            # incoming qty is binding. No default-config sizing here — sizing
            # with defaults silently diverged from the deployment policy, and
            # max(float(qty), 0.0) let NaN through to MarketOrderRequest.
            if qty is None:
                raise ValueError(
                    "Alpaca submit_order requires an explicit risk-sized qty; "
                    "size exactly once upstream and pass it as binding "
                    f"(signal_id={signal.id}, symbol={signal.symbol})."
                )
            try:
                qty_value = coerce_positive_live_qty(qty, field_name="Alpaca order qty")
            except ValueError as exc:
                raise ValueError(
                    "Alpaca order qty must be finite and positive, "
                    f"got {qty!r} (signal_id={signal.id}, symbol={signal.symbol})."
                ) from exc
            qty = qty_value

            # Must precede save_live_signal: an exit-only order with no position to
            # close is refused before the signal row is persisted, and an oversized
            # one is capped so it cannot open a new naked position.
            qty = await self._require_exit_position(signal, qty)

            order_side, time_in_force, market_request_cls = (
                self._load_alpaca_order_types()
            )

            canonical_signal_id, created = await asyncio.to_thread(
                save_live_signal, signal
            )
            if not created:
                return await self._recover_existing_signal_order(
                    trading_client, canonical_signal_id
                )

            request = self._build_order_request(
                signal,
                qty=qty,
                order_side=order_side,
                time_in_force=time_in_force,
                market_request_cls=market_request_cls,
                client_order_id=canonical_signal_id,
            )
        order = await asyncio.to_thread(trading_client.submit_order, order_data=request)
        broker_order_id = _normalize_broker_order_id(getattr(order, "id", None))
        if broker_order_id is None:
            raise RuntimeError(
                "Alpaca order response carried no valid positive broker order id "
                f"(signal_id={signal.id}, symbol={signal.symbol})."
            )
        await self._persist_submitted_order_id(canonical_signal_id, broker_order_id)
        logger.info(
            "Submitted Alpaca live order",
            extra={
                "data": {
                    "signal_id": canonical_signal_id,
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
        account = await asyncio.to_thread(self._trading_client.get_account)  # type: ignore[union-attr]  # Optional client/outcome is guarded by the caller; mypy cannot follow it
        balance = _to_float(getattr(account, "cash", None), default=0.0)
        equity = _to_float(getattr(account, "equity", None), default=balance)
        return balance, equity

    async def get_market_data(
        self,
        symbol: str,
        *,
        timeframe: str,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Recent bars, refusing to return a shorter history than requested.

        A short history is not cosmetic: indicators would be computed over fewer
        bars than the backtest used, so the live strategy would quietly behave
        differently from the one that was validated.
        """
        from alpaca.common.enums import Sort
        from alpaca.data.requests import StockBarsRequest

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required for Alpaca market data")
        resolved_timeframe, _normalized = _alpaca_timeframe(timeframe)
        bounded_limit = max(1, min(int(limit), 1000))
        if not self._connected or self._historical_data_client is None:
            raise RuntimeError(
                "Alpaca adapter must be connected before requesting market data"
            )

        def _fetch(slack_multiplier: float) -> pd.DataFrame:
            request = StockBarsRequest(
                symbol_or_symbols=[normalized_symbol],
                timeframe=resolved_timeframe,
                start=_alpaca_market_data_start(
                    timeframe, bounded_limit, slack_multiplier=slack_multiplier
                ),
                limit=1000,
                adjustment="raw",  # type: ignore[arg-type]  # value validated at runtime / narrowed by an earlier guard
                feed="iex",  # type: ignore[arg-type]  # value validated at runtime / narrowed by an earlier guard
                # Newest first. The response is capped at `limit`, so an
                # ascending request over a widened window would return the
                # oldest bars in it. Sorted back to ascending below.
                sort=Sort.DESC,
            )
            return self._historical_data_client.get_stock_bars(request).df  # type: ignore[union-attr]  # Optional client/outcome is guarded by the caller; mypy cannot follow it

        frame = await asyncio.to_thread(_fetch, 1.0)
        if frame is not None and len(frame.index) < bounded_limit:
            widened = await asyncio.to_thread(_fetch, 4.0)
            if widened is not None and len(widened.index) > len(frame.index):
                frame = widened
        if frame is not None and 0 < len(frame.index) < bounded_limit:
            raise ValueError(
                f"Alpaca returned only {len(frame.index)} of {bounded_limit} requested "
                f"{timeframe} bars for {normalized_symbol}; refusing to compute "
                "indicators on a shorter history than requested"
            )
        if frame is None or frame.empty:
            raise ValueError(
                f"Alpaca returned no {timeframe} market data for {normalized_symbol}"
            )
        frame = frame.reset_index()
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        required = {"open", "high", "low", "close"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(
                "Alpaca market data is missing columns: " + ", ".join(sorted(missing))
            )
        if "timestamp" not in frame.columns:
            raise ValueError("Alpaca market data is missing timestamp")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
        frame = frame.tail(bounded_limit).reset_index(drop=True)
        if frame.empty:
            raise ValueError(
                f"Alpaca returned no usable {timeframe} bars for {normalized_symbol}"
            )
        newest = pd.Timestamp(frame["timestamp"].iloc[-1])
        now = pd.Timestamp.now(tz="UTC")
        # Intraday bars can legitimately be days old across a market closure: a
        # Friday close to a Tuesday open is ~89 hours once a holiday Monday is
        # included, and IEX has no pre/post session to shorten the gap. 72 hours
        # rejected that valid data; a week still catches a feed that has stopped.
        max_age = (
            timedelta(days=7)
            if str(timeframe).strip().lower() == "1d"
            else timedelta(days=5)
        )
        if newest < now - max_age:
            raise ValueError(
                f"Alpaca market data is stale for {normalized_symbol}: "
                f"newest bar {newest.isoformat()} exceeds {max_age}"
            )
        return frame

    # ── Operator safety controls ──────────────────────────────────────────────────────────────
    #
    # Ordering matters more than the calls themselves. `cancel_open_orders` must never cancel
    # the SL/TP legs attached to a live position: the flatten runs after it, and if the
    # flatten fails the position would survive with every risk control removed. Alpaca has no
    # single "cancel detached orders" endpoint, so this classifies each working order by what
    # it would do to the *current* book:
    #
    #   * an order on a flat symbol cannot protect anything - cancel it (it is either an
    #     entry, or a protection orphaned by an earlier close);
    #   * an order on an open position whose side reduces that position is a protection -
    #     leave it;
    #   * anything else would open or extend exposure - cancel it.
    #
    # The flatten cannot follow that rule to the letter: Alpaca reserves the quantity of a
    # reducing-side order, so a working SL/TP makes `close_position` fail with "insufficient
    # qty available". `close_all_positions` therefore cancels each symbol's protections itself,
    # confirms the cancellation by re-read, closes, and restores a stop if the close is
    # refused - see `_flatten_symbol`.
    #
    # After the flatten, every symbol is flat, so `cancel_orphaned_protections` removes the
    # legs that outlived their position.

    @property
    def supports_cancel_orders(self) -> bool:
        return True

    @property
    def supports_flatten(self) -> bool:
        return True

    async def _require_trading_client(self) -> Any:
        if not self._connected or self._trading_client is None:
            await self.connect()
        client = self._trading_client
        if client is None:
            raise RuntimeError("Alpaca trading client is unavailable after connect.")
        return client

    async def _list_open_orders(self, client: Any) -> list[Any]:
        """Every working order, or a hard failure - never a silent empty list."""
        get_orders = getattr(client, "get_orders", None)
        if not callable(get_orders):
            raise RuntimeError(
                "Alpaca client cannot list orders; refusing to report a clean sweep"
            )
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            result = await asyncio.to_thread(get_orders, filter=request)
        except ModuleNotFoundError:
            result = await asyncio.to_thread(get_orders)
        except TypeError:
            # A client double (or an older alpaca-py) without the `filter`
            # keyword. Only retried here; an error raised *inside* get_orders is
            # not a TypeError from the call shape and propagates.
            result = await asyncio.to_thread(get_orders)
        if result is None:
            raise RuntimeError("Alpaca returned no order list; refusing to report a clean sweep")
        if isinstance(result, (list, tuple)):
            return list(result)
        for key in ("orders", "order"):
            value = _order_attr(result, key, None)
            if isinstance(value, (list, tuple)):
                return list(value)
        raise RuntimeError(
            f"Alpaca order list has an unrecognised shape ({type(result).__name__}); "
            "refusing to report a clean sweep"
        )

    @staticmethod
    def _protection_order_ids(
        orders: list[Any], protection_side_by_symbol: dict[str, str]
    ) -> set[str]:
        """Ids of orders that must survive the entry-cancel sweep.

        Two shapes mean "protection": a leg nested under a working parent, and
        an order whose side reduces an open position. The second covers the case
        where the parent has already filled and only the legs are returned.
        """
        protected: set[str] = set()
        for order in orders:
            for leg in _order_legs(order):
                leg_id = _control_order_id(_order_attr(leg, "id"))
                if leg_id is not None:
                    protected.add(leg_id)
            symbol = _order_symbol(order)
            expected_side = protection_side_by_symbol.get(symbol)
            raw_side = _order_attr(order, "side", "")
            side = str(getattr(raw_side, "value", raw_side) or "").strip().lower()
            if expected_side and side == expected_side:
                order_id = _control_order_id(_order_attr(order, "id"))
                if order_id is not None:
                    protected.add(order_id)
        return protected

    async def _protection_sides_by_symbol(self) -> dict[str, str]:
        """The side that would *reduce* each open position."""
        sides: dict[str, str] = {}
        for position in await self.get_positions():
            symbol = str(position.get("symbol") or "").strip().upper()
            qty = _position_qty(position)
            if not symbol or _is_flat(qty):
                continue
            sides[symbol] = "sell" if qty > 0 else "buy"
        return sides

    async def _cancel_one(self, client: Any, order_id: str) -> None:
        cancel = getattr(client, "cancel_order_by_id", None)
        if not callable(cancel):
            cancel = getattr(client, "cancel_order", None)
        if not callable(cancel):
            raise RuntimeError("Alpaca client cannot cancel an order by id")
        await asyncio.to_thread(cancel, order_id)

    async def cancel_open_orders(self) -> ControlOutcome:
        """Cancel working orders that could open or extend exposure.

        SL/TP legs on a live position are deliberately left alone: the flatten
        that follows is the step that removes the position, and a failure there
        must not leave it naked. A refused cancel does not stop the rest, and
        every refusal/unreadable id is reported so a partial sweep cannot be
        mistaken for a complete one.
        """
        client = await self._require_trading_client()
        orders = await self._list_open_orders(client)
        protected = self._protection_order_ids(
            orders, await self._protection_sides_by_symbol()
        )
        completed = 0
        unidentified: list[str] = []
        failed: list[str] = []
        for order in orders:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                unidentified.append(f"order:{_order_attr(order, 'id', None)!r}")
                continue
            if order_id in protected:
                continue
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:
                failed.append(f"{order_id}:{exc}")
                logger.warning(
                    "Alpaca refused to cancel order %s: %s. Continuing the sweep.",
                    order_id,
                    exc,
                )
                continue
            completed += 1

        # A cancel the broker accepted must actually disappear from the working
        # book. Re-reading also catches an order whose cancel was silently
        # dropped, which a count alone would report as done.
        try:
            remaining = await self._list_open_orders(client)
        except Exception as exc:
            return ControlOutcome(
                completed=completed,
                unidentified=tuple(unidentified),
                failed=tuple(failed) + (f"verify:{exc}",),
            )
        still_working = {
            _control_order_id(_order_attr(order, "id"))
            for order in remaining
            if _control_order_id(_order_attr(order, "id")) not in protected
        }
        still_working.discard(None)
        failed.extend(sorted(still_working))  # type: ignore[arg-type]  # value validated at runtime / narrowed by an earlier guard
        return ControlOutcome(
            completed=completed,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
        )

    @staticmethod
    def _best_stop_price(orders: list[Any], symbol: str) -> float | None:
        """The stop price to restore if a close is refused.

        Taken from the protection about to be cancelled, so the position is
        restored to the stop the strategy chose rather than one invented from a
        stale quote. A nested stop leg is checked too, because a bracket's stop
        is sometimes only visible there.
        """
        for order in orders:
            if _order_symbol(order) != symbol:
                continue
            for candidate in (order, *_order_legs(order)):
                candidate_symbol = _order_symbol(candidate)
                if candidate_symbol not in {"", symbol}:
                    continue
                price = _to_float(
                    _order_attr(candidate, "stop_price", None), default=0.0
                )
                if price > 0:
                    return price
                nested = _order_attr(candidate, "stop_loss", None)
                nested_price = _to_float(
                    _order_attr(nested, "stop_price", None), default=0.0
                )
                if nested_price > 0:
                    return nested_price
        return None

    def _load_stop_order_types(self) -> tuple[Any, Any, Any]:
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import StopOrderRequest
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        return OrderSide, TimeInForce, StopOrderRequest

    @staticmethod
    def _has_working_stop(orders: list[Any], symbol: str, side: str) -> bool:
        """Whether a stop that actually protects this position is still working.

        The side matters. A buy stop on a long is an entry for a different intent,
        not a protection: treating it as one after the position's own sell stop was
        cancelled reports the position as protected while it is naked. Only a stop
        on the reducing side counts, and a bracket's leg counts only if that leg
        itself is on the reducing side.
        """
        wanted = "buy" if str(side).strip().lower() == "buy" else "sell"
        for order in orders:
            if _order_symbol(order) != symbol:
                continue
            for candidate in (order, *_order_legs(order)):
                candidate_symbol = _order_symbol(candidate)
                if candidate_symbol not in {"", symbol}:
                    continue
                order_type = str(_order_attr(candidate, "order_type", "") or "").lower()
                if "stop" not in order_type:
                    continue
                if _order_side(candidate) == wanted:
                    return True
        return False

    async def _restore_stop(
        self,
        client: Any,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None,
    ) -> bool:
        """Put a stop back on a position whose close was refused.

        The protections were cancelled to unblock the close, so a refusal would
        otherwise leave the position naked. Restoring the stop the strategy had
        is the only acceptable outcome short of a fill; failing to do so is
        logged at CRITICAL because it means live unprotected exposure.

        Returns whether the position is protected afterwards, which is also true
        when a stop still works at the broker. Re-submitting in that case would
        leave two stops on one position, and the second becomes an orphan the
        moment the first fills - an orphaned sell-stop opens a short.
        """
        try:
            working = await self._list_open_orders(client)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not re-read orders before restoring a stop; restoring anyway",
                extra={"data": {"symbol": symbol, "error": repr(exc)}},
            )
            working = None
        if working is not None and self._has_working_stop(working, symbol, side):
            logger.warning(
                "Not restoring a stop: one still works at the broker",
                extra={"data": {"symbol": symbol}},
            )
            return True
        if stop_price is None:
            logger.critical(
                "Cannot restore a stop: the cancelled orders carried no stop price, "
                "so the position is unprotected until the next retry",
                extra={"data": {"symbol": symbol, "qty": qty}},
            )
            return False
        try:
            order_side, time_in_force, stop_request_cls = self._load_stop_order_types()
            request = stop_request_cls(
                symbol=symbol,
                qty=abs(qty),
                side=order_side.SELL if side == "sell" else order_side.BUY,
                time_in_force=time_in_force.DAY,
                stop_price=stop_price,
            )
            await asyncio.to_thread(client.submit_order, order_data=request)
        except Exception as exc:
            logger.critical(
                "Failed to restore a stop on a position whose close was refused; "
                "the position is unprotected",
                extra={
                    "data": {
                        "symbol": symbol,
                        "qty": qty,
                        "stop_price": stop_price,
                        "error": repr(exc),
                    }
                },
            )
            return False
        logger.warning(
            "Restored a stop after the broker refused to close the position",
            extra={
                "data": {"symbol": symbol, "qty": qty, "stop_price": stop_price}
            },
        )
        return True

    async def _abandon_flatten(
        self,
        client: Any,
        symbol: str,
        qty: float,
        side: str,
        stop_price: float | None,
        reason: str,
        *,
        removed_protection: bool,
    ) -> str:
        """Report why a flatten stopped, restoring the stop if one was removed.

        Once a protection has been cancelled the position is unprotected, so
        every early exit after that point has to put a stop back before the
        failure is reported.
        """
        if removed_protection:
            if not await self._restore_stop(client, symbol, qty, side, stop_price):
                return f"{reason};NO-STOP-RESTORED"
        return reason

    async def _flatten_symbol(self, client: Any, symbol: str, qty: float) -> str | None:
        """Cancel this symbol's working orders, then close its position.

        Returns ``None`` on success, or a short reason for the failure.

        The cancel is not optional. Alpaca reserves the quantity of every
        reducing-side order, so `close_position` is refused with "insufficient
        qty available" while the SL/TP legs are still working. That is the one
        place where "keep the protections until the flatten succeeds" cannot
        hold - the protections are exactly what blocks the flatten - so the stop
        is re-placed on every failure after a protection was removed.
        """
        side = "sell" if qty > 0 else "buy"
        orders = await self._list_open_orders(client)
        on_symbol = [order for order in orders if _order_symbol(order) == symbol]
        stop_price = self._best_stop_price(on_symbol, symbol)

        cancelled_any = False
        for order in on_symbol:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                return await self._abandon_flatten(
                    client,
                    symbol,
                    qty,
                    side,
                    stop_price,
                    f"unreadable-order:{_order_attr(order, 'id', None)!r}",
                    removed_protection=cancelled_any,
                )
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:
                return await self._abandon_flatten(
                    client,
                    symbol,
                    qty,
                    side,
                    stop_price,
                    f"cancel:{order_id}:{exc}",
                    removed_protection=cancelled_any,
                )
            cancelled_any = True

        # A cancel the broker accepted but did not apply still reserves the
        # quantity, and the close would then be refused. Confirm by re-read.
        try:
            remaining_orders = await self._list_open_orders(client)
        except Exception as exc:
            return await self._abandon_flatten(
                client,
                symbol,
                qty,
                side,
                stop_price,
                f"verify:{exc}",
                removed_protection=cancelled_any,
            )
        still_working = [
            _control_order_id(_order_attr(order, "id"))
            for order in remaining_orders
            if _order_symbol(order) == symbol
        ]
        if still_working:
            return await self._abandon_flatten(
                client,
                symbol,
                qty,
                side,
                stop_price,
                "orders-still-working:"
                + ",".join(str(order_id) for order_id in still_working),
                removed_protection=cancelled_any,
            )

        try:
            await asyncio.to_thread(client.close_position, symbol)
        except Exception as exc:
            return await self._abandon_flatten(
                client,
                symbol,
                qty,
                side,
                stop_price,
                f"close:{exc}",
                removed_protection=cancelled_any,
            )
        return None

    async def close_all_positions(self) -> ControlOutcome:
        """Flatten every open position, one symbol at a time.

        `close_all_positions(cancel_orders=False)` cannot do this job: it does
        not cancel the reducing-side legs, Alpaca reserves their quantity, and
        the close is refused - so a stopped deployment with a working SL/TP
        would never actually flatten. Each symbol is therefore handled on its
        own: cancel its working orders, confirm by re-read, close, and restore a
        stop if the close is refused.
        """
        client = await self._require_trading_client()
        positions = [
            position
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        ]
        if not positions:
            return ControlOutcome(completed=0)

        failed: list[str] = []
        for position in positions:
            symbol = str(position.get("symbol") or "").strip().upper()
            qty = _position_qty(position)
            if not symbol or _is_flat(qty):
                continue
            problem = await self._flatten_symbol(client, symbol, qty)
            if problem is not None:
                failed.append(f"{symbol}:{problem}")

        # The broker's acknowledgement is not proof the account is flat. A
        # position that is still open must keep the deployment unenforced.
        remaining = [
            position
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        ]
        for position in remaining:
            symbol = str(position.get("symbol") or "?")
            if not any(item.startswith(f"{symbol}:") for item in failed):
                failed.append(symbol)
        return ControlOutcome(
            completed=len(positions) - len(remaining),
            failed=tuple(failed),
        )

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        """Cancel working orders left on symbols with no open position.

        Called after a successful flatten, when every symbol is flat: what
        remains are the SL/TP legs detached from the positions they protected.
        An order on a symbol that still has a position is never touched, so this
        cannot disarm a live position.
        """
        client = await self._require_trading_client()
        orders = await self._list_open_orders(client)
        open_symbols = {
            str(position.get("symbol") or "").strip().upper()
            for position in await self.get_positions()
            if not _is_flat(_position_qty(position))
        }
        completed = 0
        unidentified: list[str] = []
        failed: list[str] = []
        for order in orders:
            order_id = _control_order_id(_order_attr(order, "id"))
            if order_id is None:
                unidentified.append(f"order:{_order_attr(order, 'id', None)!r}")
                continue
            if _order_symbol(order) in open_symbols:
                continue
            try:
                await self._cancel_one(client, order_id)
            except Exception as exc:
                failed.append(f"{order_id}:{exc}")
                continue
            completed += 1
        return ControlOutcome(
            completed=completed,
            unidentified=tuple(unidentified),
            failed=tuple(failed),
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with pricing and unrealized PnL."""
        if not self._connected or self._trading_client is None:
            await self.connect()
        rows = await asyncio.to_thread(self._trading_client.get_all_positions)  # type: ignore[union-attr]  # Optional client/outcome is guarded by the caller; mypy cannot follow it
        payload: list[dict[str, Any]] = []
        for row in rows:
            raw_qty = getattr(row, "qty", None)
            entry: dict[str, Any] = {
                "symbol": str(getattr(row, "symbol", "")).strip().upper(),
                "qty": _to_float(raw_qty, default=0.0),
                "entry_price": _to_float(getattr(row, "avg_entry_price", None), default=0.0),
                "current_price": _to_float(getattr(row, "current_price", None), default=0.0),
                "unrealized_pnl": _to_float(getattr(row, "unrealized_pl", None), default=0.0),
            }
            if raw_qty is not None and not _is_parseable_qty(raw_qty):
                # Keep the coercion for display consumers, but remember that the
                # broker's value could not be read: the risk paths that decide
                # whether an account is flat must fail closed rather than treat
                # it as a zero position.
                entry["qty_unparseable"] = True
                entry["qty_raw"] = raw_qty
            payload.append(entry)
        return payload

    def _parse_trade_update_event(
        self, data: Any
    ) -> tuple[str, Any, Any, UUID] | None:
        event = str(getattr(data, "event", "")).lower()
        if event not in {"fill", "partial_fill"}:
            return None

        order = getattr(data, "order", None)
        if order is None:
            logger.warning("Trade update without order payload")
            return None

        client_order_id = getattr(order, "client_order_id", "")
        if not client_order_id:
            logger.warning("Trade update missing client_order_id")
            return None

        try:
            signal_uuid = UUID(str(client_order_id))
        except ValueError:
            logger.warning(
                "Trade update client_order_id is not a UUID",
                extra={"data": {"client_order_id": str(client_order_id)}},
            )
            return None
        return event, order, client_order_id, signal_uuid

    def _resolve_trade_update_qty(
        self,
        data: Any,
        order: Any,
        *,
        event: str,
        client_order_id: Any,
    ) -> tuple[bool, Any]:
        filled_qty_value = getattr(data, "qty", None)
        if filled_qty_value is None:
            filled_qty_value = getattr(data, "filled_qty", None)
        if filled_qty_value is None:
            # Per-event quantity. `order.filled_qty` is CUMULATIVE in alpaca-py and
            # must not be persisted per partial-fill event (double counting).
            if event == "partial_fill":
                logger.warning(
                    "Partial fill without per-event qty; skipping to avoid "
                    "double-counting cumulative order.filled_qty",
                    extra={"data": {"client_order_id": str(client_order_id)}},
                )
                return True, None
            filled_qty_value = getattr(order, "filled_qty", None)
        return False, filled_qty_value

    async def _persist_execution_fill(
        self,
        execution: LiveExecution,
        broker_cumulative_filled_qty: float,
    ) -> float:
        if broker_cumulative_filled_qty > 0.0:
            return await asyncio.to_thread(
                save_live_execution_up_to_cumulative,
                execution,
                broker_cumulative_filled_qty,
            )
        persisted = await asyncio.to_thread(save_live_execution, execution)
        return execution.filled_qty if persisted else 0.0

    async def _on_trade_update(self, data: Any) -> None:
        try:
            parsed = self._parse_trade_update_event(data)
            if parsed is None:
                return
            event, order, client_order_id, signal_uuid = parsed
            raw_broker_order_id = getattr(order, "id", None)
            broker_order_id = _normalize_broker_order_id(raw_broker_order_id)
            if broker_order_id is None:
                logger.warning(
                    "Trade update missing valid broker order id",
                    extra={
                        "data": {
                            "account_id": self.account_id,
                            "client_order_id": str(client_order_id),
                            "broker_order_id": str(raw_broker_order_id or ""),
                        }
                    },
                )
                return

            fill_price = getattr(data, "fill_price", None)
            if fill_price is None:
                fill_price = getattr(data, "price", None)
            if fill_price is None:
                fill_price = getattr(order, "filled_avg_price", None)

            skip, filled_qty_value = self._resolve_trade_update_qty(
                data, order, event=event, client_order_id=client_order_id
            )
            if skip:
                return

            commission_value = getattr(data, "commission", None)
            if commission_value is None:
                commission_value = getattr(order, "commission", None)

            actual_price = _to_float(fill_price)
            filled_qty = _to_float(filled_qty_value)
            commission = _to_float(commission_value)
            fill_ts = _to_datetime(getattr(data, "timestamp", None))

            execution_id = str(getattr(data, "execution_id", "") or "")
            if execution_id:
                dedupe_key = f"alpaca:exec:{execution_id}"
            else:
                dedupe_key = (
                    f"alpaca:order:{broker_order_id}:{event}:"
                    f"{fill_ts.isoformat()}:{filled_qty}"
                )

            execution = LiveExecution(
                signal_id=signal_uuid,
                actual_price=actual_price,
                filled_qty=filled_qty,
                commission=commission,
                timestamp=fill_ts,
                broker_order_id=broker_order_id,
                dedupe_key=dedupe_key,
            )
            broker_cumulative_filled_qty = _to_float(
                getattr(order, "filled_qty", None), default=0.0
            )
            persisted_qty = await self._persist_execution_fill(
                execution, broker_cumulative_filled_qty
            )
            logger.info(
                (
                    "Persisted Alpaca execution fill"
                    if persisted_qty > 0.0
                    else "Skipped duplicate Alpaca execution fill"
                ),
                extra={
                    "data": {
                        "signal_id": str(execution.signal_id),
                        "execution_id": str(execution.id),
                        "event": event,
                        "actual_price": actual_price,
                        "filled_qty": filled_qty,
                        "persisted_qty": persisted_qty,
                        "broker_cumulative_filled_qty": broker_cumulative_filled_qty,
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
                # #78: subscribe exactly once per stream instance —
                # re-subscribing on every reconnect fires duplicate fill
                # callbacks (double-counted PnL/exposure).
                if self._subscribed_stream is not self._trading_stream:
                    self._trading_stream.subscribe_trade_updates(self._on_trade_update)
                    self._subscribed_stream = self._trading_stream
                # NOTE: TradingStream.run is blocking sync; it occupies one
                # executor thread for the stream lifetime (no asyncio-native
                # alternative in alpaca-py). The thread is released when run
                # returns/raises; on error the stream is stopped below before
                # discard so a healthy stream never leaks a thread per retry.
                await asyncio.to_thread(self._trading_stream.run)  # type: ignore[union-attr]  # Optional client/outcome is guarded by the caller; mypy cannot follow it
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
                stream = self._trading_stream
                self._trading_stream = None
                if self._subscribed_stream is stream:
                    self._subscribed_stream = None
                if stream is not None:
                    stop_ws = getattr(stream, "stop_ws", None)
                    stop = getattr(stream, "stop", None)
                    try:
                        if callable(stop_ws):
                            result = stop_ws()
                            if inspect.isawaitable(result):
                                await result
                        elif callable(stop):
                            result = stop()
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        pass
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
            else:
                # #78: a clean run() return must not spin — back off before
                # re-subscribing (reconnect storm) and reset the backoff.
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = 2
