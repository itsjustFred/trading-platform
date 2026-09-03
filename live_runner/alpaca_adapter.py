from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from alpaca.trading.enums import OrderClass
from alpaca.trading.requests import StopLossRequest, TakeProfitRequest

from live_runner.base_adapter import BaseLiveAdapter
from live_runner.credential_env import parse_bool_env
from risk.live import size_live_signal
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
    """Return the first parseable positive-precision float from metadata keys."""
    for key in keys:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
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


def _alpaca_credential_blocks(
    values: Mapping[str, str],
    indexes: list[int],
) -> list[dict[str, str | bool]]:
    blocks: list[dict[str, str | bool]] = []
    for idx in indexes:
        api_key = str(values.get(f"ALPACA_API_KEY_ACC{idx}", "")).strip()
        secret_key = str(values.get(f"ALPACA_SECRET_KEY_ACC{idx}", "")).strip()
        if not api_key or not secret_key:
            continue

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
        blocks.append(
            {
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
        )
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
    ):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self.paper = (
            _parse_bool_env("ALPACA_PAPER", os.environ.get("ALPACA_PAPER"), default=True)
            if paper is None
            else paper
        )
        self.account_id = account_id
        self._trading_client: Any | None = None
        self._trading_stream: Any | None = None
        self._connected = False

        if not self.api_key or not self.secret_key:
            raise ValueError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )

    async def connect(self) -> None:
        """Initialize REST and websocket clients, and verify account access."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.stream import TradingStream
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "alpaca-py is required for AlpacaLiveAdapter. Install package 'alpaca-py'."
            ) from exc
        self._trading_client = TradingClient(self.api_key, self.secret_key, paper=self.paper)
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
        broker_order_id = str(getattr(order, "id", ""))
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
        self._trading_client = None
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
        existing_order_id = await asyncio.to_thread(
            get_live_signal_broker_order_id, canonical_signal_id
        )
        if existing_order_id:
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
        recovered_order_id = str(getattr(recovered_order, "id", "")).strip()
        if not recovered_order_id:
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
        exit_only = str(metadata.get("risk_sizing_mode") or "") == "exit_only"
        missing = [
            name
            for name, value in (("stop_loss", stop_loss), ("take_profit", take_profit))
            if value is None
        ]
        if missing and not exit_only:
            raise ValueError(
                "Refusing to submit Alpaca order without "
                f"{' and '.join(missing)} (signal_id={signal.id}, "
                f"symbol={signal.symbol}); opening an unbounded position is not allowed."
            )
        if missing:
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
        if signal.action not in {"buy", "sell"}:
            # Fail closed: an unmapped action must never fall through to SELL.
            raise ValueError(f"Unsupported signal action: {signal.action}")
        stop_loss_price, take_profit_price = self._resolve_broker_sl_tp(signal)
        extended_hours = bool((signal.metadata or {}).get("extended_hours"))
        limit_price = _to_float((signal.metadata or {}).get("limit_price"), default=0.0)
        if signal.action == "buy":
            side = order_side.BUY
        else:
            side = order_side.SELL
        exit_only = str((signal.metadata or {}).get("risk_sizing_mode") or "") == "exit_only"
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
        return market_request_cls(
            time_in_force=time_in_force.DAY,
            extended_hours=False,
            order_class=OrderClass.SIMPLE if exit_only else OrderClass.BRACKET,
            take_profit=None if exit_only else TakeProfitRequest(limit_price=take_profit_price),
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
        if not broker_order_id:
            return
        persisted = await asyncio.to_thread(
            set_live_signal_broker_order_id, canonical_signal_id, broker_order_id
        )
        if not persisted:
            raise RuntimeError("Failed to persist Alpaca broker order id.")

    async def submit_order(self, signal: LiveSignal) -> str:
        if not self._connected or self._trading_client is None:
            await self.connect()
        trading_client = self._trading_client
        if trading_client is None:
            raise RuntimeError("Alpaca trading client is unavailable after connect.")

        # Use equity (not cash) for risk sizing so margin accounts with negative cash
        # still pass sizing validation when total account value is positive.
        _, equity = await self.get_balance()
        sized_order = size_live_signal(signal, account_balance=equity)
        qty = max(_to_float(signal.metadata.get("qty"), default=0.0), 0.0)
        if qty <= 0:
            raise ValueError(
                "Risk engine returned non-positive quantity; order blocked "
                f"(risk_percent={sized_order.risk_percent:.4f})."
            )

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
        broker_order_id = str(getattr(order, "id", ""))
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
            broker_order_id = str(getattr(order, "id", ""))

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
                self._trading_stream.subscribe_trade_updates(self._on_trade_update)
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
                self._trading_stream = None
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
            else:
                backoff_seconds = 2
