from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pandas as pd
from alpaca.trading.enums import OrderClass
from alpaca.trading.requests import StopLossRequest, TakeProfitRequest

from live_runner.base_adapter import (
    BaseLiveAdapter,
    coerce_positive_live_qty,
    is_exit_only_metadata,
    is_unbounded_tp,
)
from live_runner.credential_env import parse_bool_env, resolve_env_value
from shared import market_calendar
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
from shared.models import (
    SL_METADATA_KEYS,
    TP_METADATA_KEYS,
    LiveExecution,
    LiveSignal,
)

logger = get_logger("live-runner")

_ALPACA_ACC_RE = re.compile(r"^ALPACA_API_KEY_ACC(?P<index>\d+)$")

# One definition, in shared/models.py, shared with the live sizing path in
# risk/live.py. See the note there: if these drift, a backtest sizes a stop that live
# ignores.
_SL_METADATA_KEYS = SL_METADATA_KEYS
_TP_METADATA_KEYS = TP_METADATA_KEYS


def _is_order_not_found(exc: BaseException) -> bool:
    """Whether an order lookup failed because the order is genuinely absent.

    Alpaca answers 404 for a client order id it does not know, which is a real
    answer: there is no fill. A 429, a 5xx or a transport error is the broker
    failing to answer, which is not an answer at all, and must not be collapsed into
    the same case as absence.

    Read structurally rather than by exception class, because `APIError` moves
    between alpaca-py releases and this module already treats the SDK as optional.
    The status code carries the meaning, wherever the SDK hung it.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) == 404
    except (TypeError, ValueError):
        return False


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


# Nominal calendar span of one Alpaca bar, in days, keyed by the *wire* value
# of the timeframe unit (`TimeFrameUnit.Minute.value == "Min"`), not by its
# Python `name`, which is "Minute". Derived from the resolved TimeFrame rather
# than from a second string parser so it cannot drift from `_alpaca_timeframe`.
_ALPACA_UNIT_NOMINAL_DAYS = {
    "Min": 1.0 / 1440.0,
    "Hour": 1.0 / 24.0,
    "Day": 1.0,
    "Week": 7.0,
    "Month": 30.0,
}


def _now_utc() -> pd.Timestamp:
    """The clock the staleness budget is measured against.

    A named seam rather than an inline ``pd.Timestamp.now(tz="UTC")`` so a test
    can pin it: the budget's whole job is to be right *around* a market closure,
    which is not a state a test can reach by waiting for the right day.
    """
    return pd.Timestamp.now(tz="UTC")


def _alpaca_timeframe_nominal_days(resolved_timeframe: Any) -> float:
    """Calendar days one bar of ``resolved_timeframe`` covers."""
    unit = getattr(getattr(resolved_timeframe, "unit_value", None), "value", None)
    span = _ALPACA_UNIT_NOMINAL_DAYS.get(str(unit))
    if span is None:
        raise ValueError(
            f"Cannot size a staleness budget for Alpaca time unit {unit!r}"
        )
    return float(getattr(resolved_timeframe, "amount", 1)) * span


def _market_data_staleness_reason(
    newest: pd.Timestamp, now: pd.Timestamp, *, resolved_timeframe: Any
) -> str | None:
    """Why ``newest`` is too old to compute indicators on, or ``None`` if it is fine.

    Measured in **scheduled exchange sessions**, not elapsed hours. The budget
    this replaces was a flat 72 hours for intraday and 7 days for daily, and a
    flat number cannot be right for both: the US exchanges shut for a single
    weekday plus the weekend (three closed days, so the newest intraday bar is
    89.5 hours old) and for two weekdays at Thanksgiving, which pushes the flat
    budget over. It therefore raised for *every* symbol from the Thursday close
    until Tuesday of any single-holiday week, and the ``ValueError`` was
    swallowed as a rejected signal while the deployment still reported ACTIVE. A
    session count is the same answer in every week of the year, so no length
    budget is needed at all.

    ``AD_HOC_CLOSURE_TOLERANCE_SESSIONS`` covers the two things the calendar
    cannot know: a broker publishing a session's daily bar only after that
    session closed, and a closure nobody publishes in advance (a National Day
    of Mourning). See ``shared.market_calendar`` for both justifications.
    """
    behind, bar_session, expected = market_calendar.bar_staleness_sessions(
        newest.to_pydatetime(),
        now.to_pydatetime(),
        nominal_days=_alpaca_timeframe_nominal_days(resolved_timeframe),
    )
    if behind <= market_calendar.AD_HOC_CLOSURE_TOLERANCE_SESSIONS:
        return None
    return (
        f"newest bar {newest.isoformat()} belongs to session {bar_session.isoformat()} "
        f"but {expected.isoformat()} should have printed by {now.isoformat()}: "
        f"{behind} scheduled sessions behind, tolerance is "
        f"{market_calendar.AD_HOC_CLOSURE_TOLERANCE_SESSIONS}"
    )


# Markets are closed for most of the calendar: weekends alone remove two days in
# seven, and daily bars additionally lose public holidays. A flat "3x" multiplier
# under-requests intraday history, so the adapter received fewer bars than the
# caller asked for and then silently truncated to a shorter series than the
# backtest used, shifting every indicator that depends on lookback length. Size
# the window by trading time instead, and verify the result below.
_CALENDAR_TO_TRADING_SLACK = 2.2
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
        return datetime.now(timezone.utc) - timedelta(
            minutes=bars * _CALENDAR_TO_TRADING_SLACK
        )
    if normalized.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=bars * _CALENDAR_TO_TRADING_SLACK)
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

    #: Response keys under which alpaca-py reports per-item outcomes for the
    #: bulk cancel / flatten endpoints. A response matching none of them is
    #: indeterminate, not empty.
    _OPERATION_ENTRY_KEYS: tuple[str, ...] = (
        "cancelled_orders",
        "cancelled_order_ids",
        "orders",
        "symbols",
    )

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
        # Presence is checked separately from the value, for the same reason as the
        # cTrader adapter and against the same failure. `resolve_env_value` returns ""
        # both for an absent key and for one an operator deliberately blanked, and
        # keeping those apart is its whole purpose. `or None` collapsed the blanked
        # case into "absent" so the strict parse never saw the empty string.
        # `_default_alpaca_credentials` above already gets this right by testing for
        # `None` rather than falsiness; the two must not disagree about whether a
        # blanked paper flag is a refusal or a default.
        paper_configured = (env_values is not None and "ALPACA_PAPER" in env_values) or (
            "ALPACA_PAPER" in os.environ
        )
        self.paper = (
            _parse_bool_env(
                "ALPACA_PAPER",
                resolve_env_value(env_values, "ALPACA_PAPER") if paper_configured else None,
                default=True,
            )
            if paper is None
            else paper
        )
        self.account_id = account_id
        self._trading_client: Any | None = None
        self._historical_data_client: Any | None = None
        self._trading_stream: Any | None = None
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
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
            from alpaca.trading.stream import TradingStream
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        self._trading_client = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        self._historical_data_client = StockHistoricalDataClient(
            self.api_key, self.secret_key
        )
        self._trading_stream = TradingStream(self.api_key, self.secret_key, paper=self.paper)
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
                self._trading_client.get_order_by_client_id, signal_id
            )
        except Exception as exc:
            # Only a genuine "no such order" may read as "no fill". Anything else -
            # a 429, a 5xx, a dropped connection - is the broker failing to answer,
            # and treating that as absence is how a missed fill stays missed: the
            # local filled quantity never advances, the `recovered` counter stays
            # put, and `reconcile_executions` returns normally, so the caller's
            # `except` in connect() never fires and nothing is logged anywhere.
            #
            # The contrast is in this same file: `_recover_existing_signal_order`
            # makes the identical call and deliberately re-raises "so a transient
            # failure cannot be mistaken for absence", and cTrader's equivalent logs
            # before returning None. Only this path was silent.
            if _is_order_not_found(exc):
                return False
            logger.warning(
                "Could not read order state during reconciliation; a fill may be "
                "unrecorded until the next pass",
                extra={
                    "data": {
                        "account_id": self.account_id,
                        "signal_id": signal_id,
                        "error": repr(exc),
                    }
                },
            )
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
        exit_only = is_exit_only_metadata(metadata)
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
            if math.isnan(value) or math.isinf(value) or value <= 0:
                raise ValueError(
                    f"Invalid {name} price for {signal.symbol}: {value!r}; "
                    "refusing to submit."
                )
        return float(stop_loss), float(take_profit)

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
        exit_only = is_exit_only_metadata(signal.metadata)
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

    async def _require_exit_position(self, signal: LiveSignal) -> None:
        """Fail closed unless an exit-only order provably reduces a live position.

        Alpaca closes a position with an ordinary opposing market order, so
        there is no separate close endpoint to fail: an exit-only order whose
        position has already gone would silently OPEN one, and because the
        exit-only path drops the mandatory SL/TP legs it would open it
        unbounded. Refusing here is the counterpart of cTrader's
        ``_position_id_for_exit``.

        Two broker-derived refusals, both observable exceptions raised before
        anything is persisted or sent:

        1. No live position for the symbol -- there is nothing to close.
        2. The order side is not the close side for the position that exists
           (a short must be bought back, a long must be sold). The daemon
           derives that side from the same book, so a disagreement means the
           book moved under us and the order would add exposure instead of
           removing it.

        Called before ``save_live_signal``: a persisted signal row with no
        broker order wedges every redelivery on the duplicate-recovery path.
        """
        if not is_exit_only_metadata(signal.metadata):
            return
        symbol = str(signal.symbol or "").strip().upper()
        quantities = [
            quantity
            for position in await self.get_positions()
            if str(position.get("symbol") or "").strip().upper() == symbol
            and (quantity := _to_float(position.get("qty"), default=0.0)) != 0.0
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

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
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

        # Must precede save_live_signal (see B-8 above): an exit-only order with
        # no position to close is refused before the signal row is persisted.
        await self._require_exit_position(signal)

        order_side, time_in_force, market_request_cls = self._load_alpaca_order_types()

        canonical_signal_id, created = await asyncio.to_thread(save_live_signal, signal)
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
        account = await asyncio.to_thread(self._trading_client.get_account)
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
                adjustment="raw",
                feed="iex",
                # Newest first. The response is capped at `limit`, so an ascending
                # request over a widened window returns the *oldest* bars in that
                # window: widening to recover a short history would have
                # returned staler data than the first attempt. Sorted back to
                # ascending below before any consumer sees it.
                sort=Sort.DESC,
            )
            return self._historical_data_client.get_stock_bars(request).df

        # A short history is not a cosmetic problem: indicators would be computed
        # over fewer bars than the backtest used, so the live strategy would
        # quietly behave differently from the one that was validated. Widen once
        # if the first window came back short, then fail loudly rather than
        # shipping the truncated series.
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
        now = _now_utc()
        reason = _market_data_staleness_reason(
            newest, now, resolved_timeframe=resolved_timeframe
        )
        if reason is not None:
            raise ValueError(
                f"Alpaca market data is stale for {normalized_symbol}: {reason}"
            )
        return frame

    @property
    def supports_cancel_orders(self) -> bool:
        return True

    @property
    def supports_flatten(self) -> bool:
        return True

    @classmethod
    def _response_entries(cls, result: Any) -> list[Any] | None:
        """Entries of an alpaca-py response that may be a model or a dict.

        The installed client returns pydantic models, but the repository also
        supports the legacy raw-dict response shape, so both are handled.

        Returns None -- never [] -- for a shape we could not read. "I could not
        parse the response" is INDETERMINATE; "the broker had nothing to do" is
        a genuine zero. Collapsing them made ``cancel_open_orders()`` report 0
        for an unreadable response, which satisfies the daemon's
        ``cancelled is not None`` check and records a STOPPED deployment as
        enforced while its orders and positions are still live.
        """
        if result is None:
            return None
        if isinstance(result, dict):
            for key in cls._OPERATION_ENTRY_KEYS:
                value = result.get(key)
                if isinstance(value, list):
                    return list(value)
            return None
        if isinstance(result, (list, tuple)):
            return list(result)
        for key in cls._OPERATION_ENTRY_KEYS:
            value = getattr(result, key, None)
            if isinstance(value, (list, tuple)):
                return list(value)
        return None

    @classmethod
    def _response_count(cls, result: Any) -> int:
        entries = cls._response_entries(result)
        if entries is None:
            raise RuntimeError(
                f"Alpaca bulk response returned an unrecognised response shape "
                f"({type(result).__name__}); refusing to report a count."
            )
        return len(entries)

    @classmethod
    def _checked_response_operations(cls, result: Any, *, operation: str) -> list[Any]:
        """Parse a bulk response, failing closed unless it is readable and clean.

        The daemon marks a STOPPED deployment enforced based on these counts, so
        every "I do not actually know" must surface as an exception rather than
        a count. Three cases, one silent-success hole closed:

        1. An unrecognised (or absent) response shape is INDETERMINATE and
           raises -- it is not a broker report of zero.
        2. A present but non-numeric ``status`` is a FAILED operation. Skipping
           it silently counted a rejected cancel or close as a success.
        3. An out-of-range numeric status is a failure (as before).

        A *missing* status is still assumed successful: the legacy raw-dict
        shape omits the field rather than reporting a bad one.
        """
        entries = cls._response_entries(result)
        if entries is None:
            raise RuntimeError(
                f"Alpaca {operation} returned an unrecognised response shape "
                f"({type(result).__name__}); refusing to report a count. Reading "
                "it as 'nothing to do' would record a stopped deployment as "
                "enforced while its orders and positions are still live."
            )
        failures: list[str] = []
        for entry in entries:
            status = entry.get("status") if isinstance(entry, dict) else getattr(entry, "status", None)
            if status is None:
                continue
            identifier = (
                entry.get("id") if isinstance(entry, dict) else getattr(entry, "id", None)
            )
            try:
                code = int(status)
            except (TypeError, ValueError):
                failures.append(f"{identifier or '?'}={status!r}")
                continue
            if not 200 <= code < 300:
                failures.append(f"{identifier or '?'}={status}")
        if failures:
            raise RuntimeError(
                f"Alpaca {operation} did not succeed for: {', '.join(failures)}"
            )
        return entries

    @classmethod
    def _raise_on_failed_operations(cls, result: Any, *, operation: str) -> None:
        """Fail closed when the response is unreadable or the broker reports
        any item as not done."""
        cls._checked_response_operations(result, operation=operation)

    async def cancel_open_orders(self) -> int:
        """Cancel every open order for this account.

        Uses the bulk cancel endpoint so a STOPPED deployment cannot leave a
        resting entry behind that fires after the operator asked to stop.
        """
        if not self._connected or self._trading_client is None:
            await self.connect()
        if self._trading_client is None:
            raise RuntimeError("Alpaca trading client is not connected")

        result = await asyncio.to_thread(self._trading_client.cancel_orders)
        return len(self._checked_response_operations(result, operation="cancel_orders"))

    async def close_all_positions(self) -> int:
        """Flatten every open position for this account."""
        if not self._connected or self._trading_client is None:
            await self.connect()
        if self._trading_client is None:
            raise RuntimeError("Alpaca trading client is not connected")

        result = await asyncio.to_thread(self._trading_client.close_all_positions)
        return len(
            self._checked_response_operations(result, operation="close_all_positions")
        )

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with pricing and unrealized PnL."""
        if not self._connected or self._trading_client is None:
            await self.connect()
        rows = await asyncio.to_thread(self._trading_client.get_all_positions)
        payload: list[dict[str, Any]] = []
        for row in rows:
            payload.append(
                {
                    "symbol": str(getattr(row, "symbol", "")).strip().upper(),
                    "qty": _to_float(getattr(row, "qty", None), default=0.0),
                    "entry_price": _to_float(getattr(row, "avg_entry_price", None), default=0.0),
                    "current_price": _to_float(getattr(row, "current_price", None), default=0.0),
                    "unrealized_pnl": _to_float(getattr(row, "unrealized_pl", None), default=0.0),
                }
            )
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
