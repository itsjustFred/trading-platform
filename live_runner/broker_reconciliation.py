"""Periodic broker reconciliation for live deployments (#303).

The ledger is the local truth for what a deployment owns; the broker is the
truth for what is actually open. This module compares, per account (broker
positions and working orders are account-level), the two views on a schedule:

* signed ledger quantity per symbol vs the broker's net position;
* broker order ids the ledger still expects to be working vs the broker's
  working orders (position-protection legs are marked by the adapter and are
  not treated as untracked entries/exits). An expected order missing from the
  working list is confirmed against the broker's own order state first: a
  terminal order (expired DAY limit, cancelled IOC remainder, operator
  cancel) is not a mismatch, while working or unknown stays fail closed;
* protective stops vs open broker positions, for adapters that can list them.

Unreadable broker state is a mismatch (fail closed), but never flaps: a halt
requires ``consecutive_mismatches`` (default 2) consecutive bad passes, and a
clean pass resets the streak *and* releases the halt. The halt is entry-only:
it sets a persisted flag the dispatch path checks before submitting an entry
and the submission claim re-checks atomically, so exits, in-flight orders and
trailing stops keep working - a PAUSED status would have frozen all of them,
including the exits a mismatch is meant to protect. The halt and its release
are written to the live control audit trail. The latest pass is written to a
JSON report the viewer serves read-only.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from live_runner.base_adapter import (
    ORDER_STATE_TERMINAL,
    ORDER_STATE_WORKING,
    BaseLiveAdapter,
    BrokerOrderView,
    ProtectiveStop,
)
from live_runner.exit_signals import DEFAULT_QTY_TOLERANCE, usable_qty_tolerance
from shared import constants as shared_constants
from shared.database import (
    ENTRIES_HALT_ACTOR,
    get_account_net_filled_qty_by_deployment,
    list_live_account_filled_symbols,
    list_live_account_order_expectations,
    list_live_deployments,
    live_deployment_entries_halt_marker,
    record_live_control_audit,
    resume_live_deployment_entries_halt,
    set_live_deployment_entries_halt,
)
from shared.logging import get_logger
from shared.models import LiveDeploymentConfig

logger = get_logger("live-runner")

DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 300
DEFAULT_CONSECUTIVE_MISMATCHES = 2
MIN_RECONCILIATION_INTERVAL_SECONDS = 30
MAX_RECONCILIATION_INTERVAL_SECONDS = 86_400
MAX_CONSECUTIVE_MISMATCHES = 10
RECONCILIATION_REPORT_FILENAME = "broker_reconciliation_report.json"
#: Upper bound on one broker read (positions, working orders, protective
#: stops). A hung call must surface as an unreadable pass, not stall the loop
#: forever with a stale report and no alert.
BROKER_READ_TIMEOUT_SECONDS = 60.0
#: How far back an unfilled signal may still count as an order the ledger
#: expects to be working. Both live order paths are short-lived - Alpaca
#: submits DAY orders and cTrader submits IMMEDIATE_OR_CANCEL - so anything
#: older than this is a cancelled/expired/rejected historical order, not a
#: working one, and must not become a permanent ``missing_working_order``.
#: Inside the window a missing expected order is confirmed against the
#: broker's own order state before it becomes a finding.
ORDER_EXPECTATION_MAX_AGE = timedelta(days=7)
#: Cap on per-order broker state lookups in one pass. Each lookup is a broker
#: round-trip; a ledger full of stale expectations must not turn one pass
#: into hundreds of calls. Orders past the cap keep their finding (fail
#: closed).
MAX_ORDER_STATE_LOOKUPS = 10
#: One deadline for the whole per-order state-lookup phase. Each lookup still
#: has its own broker timeout, but ten slow-but-answering brokers must not
#: serialize into a pass that takes minutes to fail closed.
ORDER_STATE_LOOKUP_PHASE_TIMEOUT_SECONDS = 60.0


def _track_read_task(
    awaitable: Awaitable[Any], pending: set[asyncio.Task[Any]] | None
) -> asyncio.Task[Any]:
    """Start a broker read as a tracked task.

    The reads run in ``asyncio.to_thread``: ``wait_for`` cancels the coroutine
    on timeout but cannot reclaim the worker thread, which keeps running
    (possibly forever on a hung socket). Shielding keeps the task - and so the
    pending set - alive until the worker actually finishes; the account's next
    pass then refuses to stack more reads on a starved executor pool.
    """
    task = asyncio.ensure_future(awaitable)
    if pending is not None:
        pending.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            pending.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                logger.warning(
                    "Broker read finished after its waiter timed out",
                    extra={"data": {"error": str(exc)}},
                )

        task.add_done_callback(_done)
    return task


async def _shielded_read(
    awaitable: Awaitable[Any],
    timeout: float,
    pending: set[asyncio.Task[Any]] | None,
) -> Any:
    """``wait_for`` that leaves the read task running (tracked) on timeout."""
    task = _track_read_task(awaitable, pending)
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


def default_reconciliation_report_path() -> Path:
    """Where the daemon writes the latest read-only reconciliation report."""
    return shared_constants.DATA_DIR / "live" / RECONCILIATION_REPORT_FILENAME


@dataclass(frozen=True, slots=True)
class BrokerReconciliationConfig:
    """Effective per-account reconciliation settings (#303).

    ``qty_tolerance`` of ``None`` means "ask the adapter for the instrument's
    precision" (with the strict default when it cannot say).
    ``qty_tolerance_includes_adapter_default`` records that at least one
    enabled deployment left the tolerance unset, so the adapter/default value
    participates even when another deployment configured a looser explicit
    tolerance. ``interval_seconds`` is the sleep between passes; the loop uses
    the smallest configured interval across accounts so no account waits
    longer than it asked.
    """

    interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS
    qty_tolerance: float | None = None
    qty_tolerance_includes_adapter_default: bool = False
    consecutive_mismatches: int = DEFAULT_CONSECUTIVE_MISMATCHES
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One structured difference between the ledger and the broker."""

    kind: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class AccountReconciliationReport:
    """Read-only result for one account's latest pass."""

    account_id: str
    generated_at: datetime
    ok: bool
    unreadable: bool
    findings: tuple[ReconciliationFinding, ...] = ()
    consecutive_mismatches: int = 0
    required_mismatches: int = DEFAULT_CONSECUTIVE_MISMATCHES
    entries_halted_deployment_ids: tuple[str, ...] = ()
    resume_failed_deployment_ids: tuple[str, ...] = ()

    @property
    def mismatches(self) -> bool:
        return self.unreadable or bool(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "generated_at": self.generated_at.isoformat(),
            "ok": self.ok,
            "unreadable": self.unreadable,
            "mismatches": self.mismatches,
            "consecutive_mismatches": self.consecutive_mismatches,
            "required_mismatches": self.required_mismatches,
            "entries_halted_deployment_ids": list(self.entries_halted_deployment_ids),
            "resume_failed_deployment_ids": list(self.resume_failed_deployment_ids),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class BrokerStateSnapshot:
    """One account's broker view, or the reason it could not be read."""

    positions: tuple[dict[str, Any], ...] = ()
    orders: tuple[BrokerOrderView, ...] = ()
    protective_stops: tuple[ProtectiveStop, ...] = ()
    protective_stops_supported: bool = False
    error: str | None = None

    @property
    def readable(self) -> bool:
        return self.error is None


HaltEntries = Callable[[str, Mapping[str, Any]], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_SETTING_TRUE = {"1", "true", "yes", "on"}
_SETTING_FALSE = {"0", "false", "no", "off"}


def _setting_bool(settings: Mapping[str, Any], key: str, *, default: bool) -> bool:
    raw = settings.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _SETTING_TRUE:
        return True
    if text in _SETTING_FALSE:
        return False
    # Fail closed: an unrecognised value (a typo like "flase") must not
    # silently *disable* reconciliation - fall back to the safe default.
    logger.warning(
        "Ignoring unrecognised broker reconciliation boolean setting",
        extra={"data": {"key": key, "value": raw, "using": default}},
    )
    return default


def _setting_int(
    settings: Mapping[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = settings.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if not minimum <= value <= maximum:
        value = default
    if value != raw:
        logger.warning(
            "Ignoring unusable broker reconciliation setting",
            extra={"data": {"key": key, "value": raw, "using": value}},
        )
    return value


def reconciliation_config_for_deployment(
    deployment: LiveDeploymentConfig,
) -> BrokerReconciliationConfig:
    """Read one deployment's reconciliation settings, failing closed on bad values."""
    settings = dict(deployment.settings or {})
    interval = _setting_int(
        settings,
        "broker_reconciliation_interval_seconds",
        default=DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
        minimum=MIN_RECONCILIATION_INTERVAL_SECONDS,
        maximum=MAX_RECONCILIATION_INTERVAL_SECONDS,
    )
    raw_tolerance = settings.get("broker_reconciliation_qty_tolerance")
    tolerance: float | None = None
    if raw_tolerance is not None:
        tolerance = usable_qty_tolerance(raw_tolerance)
        if tolerance == DEFAULT_QTY_TOLERANCE and raw_tolerance != DEFAULT_QTY_TOLERANCE:
            # usable_qty_tolerance already failed closed to the strict default;
            # surface that the configured value was not honoured.
            logger.warning(
                "Ignoring unusable broker reconciliation qty tolerance",
                extra={"data": {"deployment_id": str(deployment.id), "value": raw_tolerance}},
            )
    consecutive = _setting_int(
        settings,
        "broker_reconciliation_consecutive_mismatches",
        default=DEFAULT_CONSECUTIVE_MISMATCHES,
        minimum=1,
        maximum=MAX_CONSECUTIVE_MISMATCHES,
    )
    return BrokerReconciliationConfig(
        interval_seconds=interval,
        qty_tolerance=tolerance,
        qty_tolerance_includes_adapter_default=tolerance is None,
        consecutive_mismatches=consecutive,
        enabled=_setting_bool(settings, "broker_reconciliation_enabled", default=True),
    )


def validate_broker_reconciliation_settings(settings: Mapping[str, Any] | None) -> None:
    """Refuse unusable reconciliation settings at deployment creation (#303).

    Runtime parsing fails closed on bad values, but a typo stored in the
    database is still a configuration error the operator should see when they
    write it, not months later in a log line. Mirrors what
    :func:`reconciliation_config_for_deployment` accepts.
    """
    from live_runner.exit_signals import MAX_QTY_TOLERANCE

    values = settings or {}
    for key, _default, minimum, maximum in (
        (
            "broker_reconciliation_interval_seconds",
            DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
            MIN_RECONCILIATION_INTERVAL_SECONDS,
            MAX_RECONCILIATION_INTERVAL_SECONDS,
        ),
        (
            "broker_reconciliation_consecutive_mismatches",
            DEFAULT_CONSECUTIVE_MISMATCHES,
            1,
            MAX_CONSECUTIVE_MISMATCHES,
        ),
    ):
        raw = values.get(key)
        if raw is None:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{key} must be an integer within [{minimum}, {maximum}], got {raw!r}"
            ) from exc
        if isinstance(raw, bool) or not minimum <= parsed <= maximum:
            raise ValueError(
                f"{key} must be an integer within [{minimum}, {maximum}], got {raw!r}"
            )
    raw_enabled = values.get("broker_reconciliation_enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        if str(raw_enabled).strip().lower() not in _SETTING_TRUE | _SETTING_FALSE:
            raise ValueError(
                "broker_reconciliation_enabled must be a boolean or one of "
                f"{sorted(_SETTING_TRUE | _SETTING_FALSE)}, got {raw_enabled!r}"
            )
    raw_tolerance = values.get("broker_reconciliation_qty_tolerance")
    if raw_tolerance is not None:
        if (
            isinstance(raw_tolerance, bool)
            or not isinstance(raw_tolerance, (int, float))
            or not math.isfinite(float(raw_tolerance))
            or not 0.0 <= float(raw_tolerance) <= MAX_QTY_TOLERANCE
        ):
            raise ValueError(
                "broker_reconciliation_qty_tolerance must be a finite number "
                f"within [0, {MAX_QTY_TOLERANCE}], got {raw_tolerance!r}"
            )


def effective_reconciliation_config(
    deployments: Iterable[LiveDeploymentConfig], account_id: str
) -> BrokerReconciliationConfig | None:
    """The strictest account-level config across its deployments, or None when disabled.

    Broker state is account-level, so one deployment's looser setting must not
    widen another's. The account reconciles unless *every* deployment on it
    opted out; a single opted-in deployment keeps the account under watch.
    """
    configs = [
        reconciliation_config_for_deployment(deployment)
        for deployment in deployments
        if str(deployment.account_id) == str(account_id)
    ]
    if not configs or all(not config.enabled for config in configs):
        return None
    enabled_configs = [config for config in configs if config.enabled]
    tolerances = [
        config.qty_tolerance
        for config in enabled_configs
        if config.qty_tolerance is not None
    ]
    return BrokerReconciliationConfig(
        interval_seconds=min(config.interval_seconds for config in enabled_configs),
        qty_tolerance=min(tolerances) if tolerances else None,
        qty_tolerance_includes_adapter_default=any(
            config.qty_tolerance is None for config in enabled_configs
        ),
        consecutive_mismatches=min(config.consecutive_mismatches for config in enabled_configs),
        enabled=True,
    )


def _requested_qty(metadata: Mapping[str, Any]) -> float | None:
    for key in ("explicit_qty", "qty", "requested_qty"):
        raw = metadata.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def _position_qty_tolerance(
    adapter: BaseLiveAdapter,
    symbol: str,
    configured: float | None,
    *,
    include_adapter_default: bool = False,
) -> float:
    method = getattr(adapter, "position_qty_tolerance", None)
    precision: float | None = None
    if callable(method):
        try:
            precision = usable_qty_tolerance(method(symbol))
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Adapter could not report quantity precision; using strict default",
                extra={"data": {"symbol": symbol, "error": str(exc)}},
            )
    if configured is not None:
        configured_tolerance = usable_qty_tolerance(configured)
        # An explicit tolerance may tighten the check but must never loosen it
        # past the precision the adapter can actually represent. When another
        # deployment on the account left the tolerance unset, the strict
        # default is the floor for the cases where the adapter cannot say.
        if precision is not None:
            return min(configured_tolerance, precision)
        if include_adapter_default:
            return min(configured_tolerance, usable_qty_tolerance(DEFAULT_QTY_TOLERANCE))
        return configured_tolerance
    if precision is not None:
        return precision
    return usable_qty_tolerance(DEFAULT_QTY_TOLERANCE)


def _parse_positions(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, float], str | None]:
    """Net signed qty per symbol, or an unreadable reason."""
    net: dict[str, float] = {}
    for row in rows:
        if row.get("qty_unparseable"):
            return {}, f"position {row.get('symbol')!r} has an unparseable qty"
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            return {}, "position row has no symbol"
        raw_qty = row.get("qty")
        if raw_qty is None or isinstance(raw_qty, bool):
            return {}, f"position {symbol} has no readable qty"
        try:
            qty = float(raw_qty)
        except (TypeError, ValueError):
            return {}, f"position {symbol} has an unparseable qty"
        if not math.isfinite(qty):
            return {}, f"position {symbol} has a non-finite qty"
        net[symbol] = net.get(symbol, 0.0) + qty
    return {symbol: qty for symbol, qty in net.items() if abs(qty) > 0}, None


async def _broker_state(
    adapter: BaseLiveAdapter, *, pending: set[asyncio.Task[Any]] | None = None
) -> BrokerStateSnapshot:
    """Read positions, working orders and protective stops; any failure is unreadable."""
    get_positions = getattr(adapter, "get_positions", None)
    if not callable(get_positions):
        return BrokerStateSnapshot(
            error="positions unreadable: adapter cannot report positions"
        )
    try:
        positions = tuple(
            await _shielded_read(
                get_positions(),
                BROKER_READ_TIMEOUT_SECONDS,
                pending,
            )
        )
    except TimeoutError:
        return BrokerStateSnapshot(
            error=f"positions unreadable: timed out after {BROKER_READ_TIMEOUT_SECONDS}s"
        )
    except Exception as exc:  # except-kind: money-decision
        return BrokerStateSnapshot(error=f"positions unreadable: {exc}")
    try:
        orders = tuple(
            await _shielded_read(
                adapter.list_reconciliation_orders(),
                BROKER_READ_TIMEOUT_SECONDS,
                pending,
            )
        )
    except NotImplementedError as exc:
        return BrokerStateSnapshot(error=f"working orders unreadable: {exc}")
    except TimeoutError:
        return BrokerStateSnapshot(
            error=(
                f"working orders unreadable: timed out after "
                f"{BROKER_READ_TIMEOUT_SECONDS}s"
            )
        )
    except Exception as exc:  # except-kind: money-decision
        return BrokerStateSnapshot(error=f"working orders unreadable: {exc}")

    stops: tuple[ProtectiveStop, ...] = ()
    stops_supported = False
    list_stops = getattr(adapter, "list_protective_stops", None)
    if callable(list_stops):
        try:
            stops = tuple(
                await _shielded_read(
                    list_stops(), BROKER_READ_TIMEOUT_SECONDS, pending
                )
            )
            stops_supported = True
        except NotImplementedError:
            stops_supported = False
        except TimeoutError:
            return BrokerStateSnapshot(
                positions=positions,
                orders=orders,
                error=(
                    f"protective stops unreadable: timed out after "
                    f"{BROKER_READ_TIMEOUT_SECONDS}s"
                ),
            )
        except Exception as exc:  # except-kind: money-decision
            return BrokerStateSnapshot(
                positions=positions,
                orders=orders,
                error=f"protective stops unreadable: {exc}",
            )
    return BrokerStateSnapshot(
        positions=positions,
        orders=orders,
        protective_stops=stops,
        protective_stops_supported=stops_supported,
    )


def _order_expectations(
    rows: Iterable[Mapping[str, Any]],
    tolerance_for: Callable[[str], float],
    *,
    expected_deployment_ids: set[str],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Signals the ledger still expects to be working, and tracked broker ids.

    Every persisted id stays *tracked* (a working broker order the ledger knows
    is never "untracked"), but only rows that can still be working count as
    *expected*: the deployment must be one reconciliation is watching
    (ACTIVE/PAUSED - a STOPPED or deleted deployment's leftovers were already
    enforced away) and the signal must be younger than
    ``ORDER_EXPECTATION_MAX_AGE``. Both live order paths expire fast (Alpaca
    DAY, cTrader IOC), so an older unfilled row is a cancelled, expired or
    rejected historical order - expecting it forever would pause a healthy
    account on every pass. Inside the window the broker's own order state
    decides: a terminal order is not a mismatch.
    """
    expected: list[dict[str, Any]] = []
    tracked_ids: set[str] = set()
    cutoff = now - ORDER_EXPECTATION_MAX_AGE
    for row in rows:
        signal_id = str(row.get("signal_id") or "").strip()
        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if signal_id:
            tracked_ids.add(signal_id)
        if broker_order_id:
            tracked_ids.add(broker_order_id)
        deployment_id = str(row.get("deployment_id") or "").strip()
        if deployment_id not in expected_deployment_ids:
            continue
        timestamp = row.get("timestamp")
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp < cutoff:
                continue
        if not broker_order_id:
            continue
        requested = _requested_qty(row.get("metadata") or {})
        filled = float(row.get("filled_qty") or 0.0)
        symbol = str(row.get("symbol") or "").strip().upper()
        tolerance = tolerance_for(symbol) if symbol else DEFAULT_QTY_TOLERANCE
        remaining = None if requested is None else requested - filled
        if remaining is not None and remaining <= tolerance:
            continue
        expected.append(
            {
                "signal_id": signal_id,
                "deployment_id": str(row.get("deployment_id") or ""),
                "symbol": symbol,
                "action": str(row.get("action") or "").lower(),
                "broker_order_id": broker_order_id,
                "requested_qty": requested,
                "filled_qty": filled,
                "remaining_qty": remaining,
            }
        )
    return expected, sorted(tracked_ids)


def _finding(kind: str, **detail: Any) -> ReconciliationFinding:
    return ReconciliationFinding(kind=kind, detail=detail)


def compare_account_state(
    *,
    account_id: str,
    snapshot: BrokerStateSnapshot,
    deployments: Iterable[LiveDeploymentConfig],
    config: BrokerReconciliationConfig,
    adapter: BaseLiveAdapter,
) -> tuple[list[ReconciliationFinding], dict[str, Any]]:
    """Diff one account's ledger against its broker snapshot."""
    if not snapshot.readable:
        return [
            _finding("broker_state_unreadable", account_id=account_id, error=snapshot.error)
        ], {}

    broker_positions, position_error = _parse_positions(snapshot.positions)
    if position_error is not None:
        return [
            _finding("broker_state_unreadable", account_id=account_id, error=position_error)
        ], {}

    findings: list[ReconciliationFinding] = []
    summary: dict[str, Any] = {"broker_positions": broker_positions}

    filled_symbols = set(list_live_account_filled_symbols(account_id))
    order_rows = list_live_account_order_expectations(account_id)
    symbols = set(broker_positions) | filled_symbols | {
        str(row.get("symbol") or "").strip().upper()
        for row in order_rows
        if str(row.get("symbol") or "").strip()
    }

    def tolerance_for(symbol: str) -> float:
        return _position_qty_tolerance(
            adapter,
            symbol,
            config.qty_tolerance,
            include_adapter_default=config.qty_tolerance_includes_adapter_default,
        )

    expected_orders, tracked_ids = _order_expectations(
        order_rows,
        tolerance_for,
        expected_deployment_ids={str(deployment.id) for deployment in deployments},
        now=_utcnow(),
    )
    summary["expected_working_orders"] = expected_orders

    for symbol in sorted(symbols):
        broker_qty = broker_positions.get(symbol, 0.0)
        ledgers = get_account_net_filled_qty_by_deployment(account_id, symbol)
        ledger_total = math.fsum(float(qty) for qty in ledgers.values())
        tolerance = tolerance_for(symbol)
        if abs(broker_qty - ledger_total) > tolerance:
            findings.append(
                _finding(
                    "position_qty_mismatch",
                    account_id=account_id,
                    symbol=symbol,
                    broker_qty=broker_qty,
                    ledger_qty_total=ledger_total,
                    ledger_qty_by_deployment=ledgers,
                    qty_tolerance=tolerance,
                )
            )

    broker_order_ids: set[str] = set()
    broker_non_protection_ids: set[str] = set()
    for order in snapshot.orders:
        ids = {str(order.order_id or "").strip(), str(order.client_order_id or "").strip()}
        ids.discard("")
        broker_order_ids.update(ids)
        if not order.protection:
            broker_non_protection_ids.update(ids)
    tracked_set = set(tracked_ids)
    # ``protection`` only suppresses the *untracked* finding: an order the
    # ledger expects may legitimately be flagged protection (a reduce-side
    # exit can look like one to the adapter), so expectations match against
    # every broker id, protection marks included.

    for order in snapshot.orders:
        if order.protection:
            continue
        ids = {str(order.order_id or "").strip(), str(order.client_order_id or "").strip()}
        ids.discard("")
        if not ids or not (ids & tracked_set):
            findings.append(
                _finding(
                    "untracked_working_order",
                    account_id=account_id,
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    order_type=order.order_type,
                )
            )

    for expected in expected_orders:
        ids = {expected["broker_order_id"], expected["signal_id"]}
        ids.discard("")
        if not (ids & broker_order_ids):
            findings.append(
                _finding(
                    "missing_working_order",
                    account_id=account_id,
                    signal_id=expected["signal_id"],
                    deployment_id=expected["deployment_id"],
                    symbol=expected["symbol"],
                    broker_order_id=expected["broker_order_id"],
                    requested_qty=expected["requested_qty"],
                    filled_qty=expected["filled_qty"],
                )
            )

    if snapshot.protective_stops_supported:
        position_symbols = {
            symbol for symbol, qty in broker_positions.items() if abs(qty) > 0
        }
        stop_symbols = {
            str(stop.symbol or "").strip().upper()
            for stop in snapshot.protective_stops
            if str(stop.symbol or "").strip()
        }
        findings.extend(
            _finding(
                "position_without_protective_stop",
                account_id=account_id,
                symbol=symbol,
                broker_qty=broker_positions[symbol],
            )
            for symbol in sorted(position_symbols - stop_symbols)
        )
        findings.extend(
            _finding(
                "orphaned_protective_stop",
                account_id=account_id,
                symbol=symbol,
            )
            for symbol in sorted(stop_symbols - position_symbols)
        )

    return findings, summary


async def _default_halt_entries(deployment_id: str, detail: Mapping[str, Any]) -> None:
    await asyncio.to_thread(set_live_deployment_entries_halt, deployment_id, detail)


async def _audit_entries_halt(
    action: str, deployment_id: str, account_id: str, **detail: Any
) -> None:
    await asyncio.to_thread(
        record_live_control_audit,
        action=action,
        target_type="live_deployment",
        target_id=deployment_id,
        actor=ENTRIES_HALT_ACTOR,
        detail={"account_id": account_id, **detail},
    )


class BrokerReconciliationManager:
    """Holds consecutive-mismatch streaks and the latest report per account."""

    def __init__(
        self,
        *,
        report_path: Path | None = None,
        halt_entries: HaltEntries | None = None,
    ) -> None:
        self._streaks: dict[str, int] = {}
        self._reports: dict[str, AccountReconciliationReport] = {}
        self._report_path = report_path or default_reconciliation_report_path()
        self._halt_entries = halt_entries or _default_halt_entries
        # Broker read tasks still running past their timeout, per account. A
        # non-empty set means a worker thread is still alive: the next pass
        # for that account is unreadable instead of stacking more threads.
        self._pending_reads: dict[str, set[asyncio.Task[Any]]] = {}

    @property
    def report_path(self) -> Path:
        return self._report_path

    def latest_report(self) -> dict[str, Any]:
        return {
            "generated_at": _utcnow().isoformat(),
            "accounts": {
                account_id: report.to_dict() for account_id, report in self._reports.items()
            },
        }

    def write_report(self) -> None:
        path = self._report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.latest_report(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _active_deployments(
        deployments: Iterable[LiveDeploymentConfig],
    ) -> list[LiveDeploymentConfig]:
        return [
            deployment
            for deployment in deployments
            if str(deployment.status or "").upper() == "ACTIVE"
        ]

    async def _lookup_order_state(
        self,
        lookup: Callable[..., Awaitable[Any]],
        order_id: str,
        client_order_id: str,
        pending: set[asyncio.Task[Any]],
        *,
        timeout: float = BROKER_READ_TIMEOUT_SECONDS,
    ) -> str | None:
        """One shielded broker order-state lookup; ``None`` on any failure."""
        primary = order_id or client_order_id
        if not primary:
            return None
        try:
            state = await _shielded_read(
                lookup(primary, client_order_id=client_order_id or primary),
                timeout,
                pending,
            )
        except Exception:  # except-kind: money-decision
            return None
        return state if state in (ORDER_STATE_WORKING, ORDER_STATE_TERMINAL) else None

    async def _resolve_terminal_missing_orders(
        self,
        account_id: str,
        adapter: BaseLiveAdapter,
        findings: list[ReconciliationFinding],
        summary: dict[str, Any],
        pending: set[asyncio.Task[Any]],
    ) -> list[ReconciliationFinding]:
        """Drop ``missing_working_order`` findings the broker proves terminal.

        A signal younger than ``ORDER_EXPECTATION_MAX_AGE`` whose order is
        absent from the working list is *expected* to be working - but Alpaca
        DAY limits expire at the close, cTrader IOC remainders are cancelled
        within milliseconds, and operators can cancel. When the adapter can
        classify the order, a terminal state is not a mismatch (an unbooked
        fill already surfaces as ``position_qty_mismatch``); working or
        unknown keeps the finding. Lookups share the pass's pending-read set:
        a still-running earlier read skips the rest, and a hung lookup leaves
        its task tracked so the next pass fails closed instead of stacking
        threads.
        """
        if not any(f.kind == "missing_working_order" for f in findings):
            return findings
        lookup = getattr(adapter, "get_order_state", None)
        if not callable(lookup):
            return findings
        kept: list[ReconciliationFinding] = []
        terminal_ids: list[str] = []
        lookups = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ORDER_STATE_LOOKUP_PHASE_TIMEOUT_SECONDS
        for finding in findings:
            if finding.kind != "missing_working_order":
                kept.append(finding)
                continue
            detail = dict(finding.detail)
            remaining = deadline - loop.time()
            if (
                lookups >= MAX_ORDER_STATE_LOOKUPS
                or remaining <= 0
                or any(not task.done() for task in pending)
            ):
                kept.append(finding)
                continue
            lookups += 1
            order_id = str(detail.get("broker_order_id") or "").strip()
            signal_id = str(detail.get("signal_id") or "").strip()
            state = await self._lookup_order_state(
                lookup,
                order_id,
                signal_id,
                pending,
                timeout=min(remaining, BROKER_READ_TIMEOUT_SECONDS),
            )
            detail["broker_order_state"] = state or "unknown"
            if state == ORDER_STATE_TERMINAL:
                terminal_ids.append(order_id or signal_id)
                continue
            kept.append(ReconciliationFinding(kind=finding.kind, detail=detail))
        if terminal_ids:
            summary["expected_orders_terminal_at_broker"] = terminal_ids
            logger.info(
                "Broker reconciliation: expected orders are terminal at the broker",
                extra={
                    "data": {
                        "account_id": account_id,
                        "order_ids": terminal_ids,
                    }
                },
            )
        return kept

    async def reconcile_account(
        self,
        *,
        account_id: str,
        deployments: list[LiveDeploymentConfig],
        adapter: BaseLiveAdapter | None,
        config: BrokerReconciliationConfig,
    ) -> AccountReconciliationReport:
        generated_at = _utcnow()
        resume_failures: list[str] = []
        if adapter is None:
            findings = [
                _finding("broker_state_unreadable", account_id=account_id, error="no adapter")
            ]
            summary: dict[str, Any] = {}
        else:
            pending = self._pending_reads.setdefault(account_id, set())
            for done_task in [task for task in pending if task.done()]:
                pending.discard(done_task)
            if pending:
                # A previous read's worker thread is still alive (a hung broker
                # call the timeout could not reclaim). Starting another pass
                # would stack more threads on the same executor pool, so this
                # pass is unreadable instead - which fails closed into the
                # streak and, eventually, the entry halt.
                snapshot = BrokerStateSnapshot(
                    error="a previous broker read for this account is still in flight"
                )
            else:
                snapshot = await _broker_state(adapter, pending=pending)
            try:
                findings, summary = await asyncio.to_thread(
                    compare_account_state,
                    account_id=account_id,
                    snapshot=snapshot,
                    deployments=deployments,
                    config=config,
                    adapter=adapter,
                )
            except Exception as exc:  # except-kind: money-decision
                # A comparison failure (DB error, malformed stored row) is
                # unreadable broker-vs-ledger state: it must count as a
                # mismatch pass, not escape the loop and silently skip the
                # streak, the report and the alert.
                logger.warning(
                    "Broker reconciliation comparison failed",
                    extra={
                        "data": {
                            "alert": "broker_reconciliation_compare_failed",
                            "account_id": account_id,
                            "error": str(exc),
                        }
                    },
                )
                findings = [
                    _finding(
                        "broker_state_unreadable",
                        account_id=account_id,
                        error=f"comparison failed: {exc}",
                    )
                ]
                summary = {}
            findings = await self._resolve_terminal_missing_orders(
                account_id, adapter, findings, summary, pending
            )

        mismatched = bool(findings)
        streak = self._streaks.get(account_id, 0) + 1 if mismatched else 0
        self._streaks[account_id] = streak

        halted: list[str] = []
        active = self._active_deployments(deployments)
        should_halt = mismatched and streak >= config.consecutive_mismatches and bool(active)
        if should_halt:
            for deployment in active:
                deployment_id = str(deployment.id)
                try:
                    marker = await asyncio.to_thread(
                        live_deployment_entries_halt_marker, deployment_id
                    )
                    if isinstance(marker, Mapping) and marker.get("by") == ENTRIES_HALT_ACTOR:
                        # The halt is already in force. Rewriting the marker
                        # and auditing on every mismatched pass would spam the
                        # control trail and keep bumping ``at``; the report
                        # and the mismatch log still carry the live streak.
                        halted.append(deployment_id)
                        continue
                    await self._halt_entries(
                        deployment_id,
                        {
                            "by": ENTRIES_HALT_ACTOR,
                            "at": _utcnow().isoformat(),
                            "account_id": account_id,
                            "consecutive_mismatches": streak,
                        },
                    )
                    halted.append(deployment_id)
                    await _audit_entries_halt(
                        "broker_reconciliation_entries_halt",
                        deployment_id,
                        account_id,
                        consecutive_mismatches=streak,
                    )
                except Exception as exc:  # except-kind: money-decision
                    logger.error(
                        "Broker reconciliation could not halt deployment entries",
                        extra={
                            "data": {
                                "alert": "broker_reconciliation_halt_failed",
                                "account_id": account_id,
                                "deployment_id": deployment_id,
                                "error": str(exc),
                            }
                        },
                    )
                    findings.append(
                        _finding(
                            "halt_failed",
                            account_id=account_id,
                            deployment_id=deployment_id,
                            error=str(exc),
                        )
                    )
        elif not mismatched:
            # A clean pass releases a halt the reconciler itself set. The
            # helper only clears a marker it owns, so an operator's own flag
            # (or a halt on a deployment that left ACTIVE/PAUSED) is untouched.
            for deployment in deployments:
                deployment_id = str(deployment.id)
                try:
                    if await asyncio.to_thread(
                        resume_live_deployment_entries_halt, deployment_id
                    ):
                        await _audit_entries_halt(
                            "broker_reconciliation_entries_resume",
                            deployment_id,
                            account_id,
                        )
                except Exception as exc:  # except-kind: money-decision
                    resume_failures.append(deployment_id)
                    logger.error(
                        "Broker reconciliation could not release an entry halt; "
                        "entries stay halted on a clean account",
                        extra={
                            "data": {
                                "alert": "broker_reconciliation_resume_failed",
                                "account_id": account_id,
                                "deployment_id": deployment_id,
                                "error": str(exc),
                            }
                        },
                    )

        if mismatched:
            log = logger.error if streak >= config.consecutive_mismatches else logger.warning
            log(
                "Broker reconciliation found a mismatch"
                if streak >= config.consecutive_mismatches
                else "Broker reconciliation mismatch below pause threshold",
                extra={
                    "data": {
                        "alert": "broker_reconciliation_mismatch",
                        "account_id": account_id,
                        "consecutive_mismatches": streak,
                        "required_mismatches": config.consecutive_mismatches,
                        "entries_halted_deployment_ids": halted,
                        "summary": summary,
                        "findings": [finding.to_dict() for finding in findings],
                    }
                },
            )
        elif streak == 0 and account_id in self._reports:
            logger.info(
                "Broker reconciliation recovered",
                extra={"data": {"account_id": account_id}},
            )

        report = AccountReconciliationReport(
            account_id=account_id,
            generated_at=generated_at,
            ok=not mismatched and not resume_failures,
            unreadable=any(f.kind == "broker_state_unreadable" for f in findings),
            findings=tuple(findings),
            consecutive_mismatches=streak,
            required_mismatches=config.consecutive_mismatches,
            entries_halted_deployment_ids=tuple(halted),
            resume_failed_deployment_ids=tuple(resume_failures),
        )
        self._reports[account_id] = report
        return report

    async def reconcile_once(
        self,
        deployments: Iterable[LiveDeploymentConfig],
        adapters: Mapping[str, BaseLiveAdapter],
    ) -> dict[str, AccountReconciliationReport]:
        deployment_list = list(deployments)
        by_account: dict[str, list[LiveDeploymentConfig]] = {}
        for deployment in deployment_list:
            status = str(deployment.status or "").upper()
            if status not in {"ACTIVE", "PAUSED"}:
                continue
            by_account.setdefault(str(deployment.account_id), []).append(deployment)

        reports: dict[str, AccountReconciliationReport] = {}
        for account_id in sorted(by_account):
            account_deployments = by_account[account_id]
            config = effective_reconciliation_config(account_deployments, account_id)
            if config is None:
                self._streaks.pop(account_id, None)
                self._reports.pop(account_id, None)
                continue
            report = await self.reconcile_account(
                account_id=account_id,
                deployments=account_deployments,
                adapter=adapters.get(account_id),
                config=config,
            )
            reports[account_id] = report
        # Accounts no longer reconciled (deployments deleted, stopped, or moved)
        # must not keep a streak or a served report: a recreated deployment
        # would otherwise inherit the old account's mismatch count.
        for account_id in [a for a in self._streaks if a not in by_account]:
            self._streaks.pop(account_id, None)
        for account_id in [a for a in self._reports if a not in by_account]:
            self._reports.pop(account_id, None)
        self.write_report()
        return reports

    def next_interval_seconds(self, deployments: Iterable[LiveDeploymentConfig]) -> int:
        deployment_list = list(deployments)
        intervals = [
            config.interval_seconds
            for deployment in deployment_list
            if str(deployment.status or "").upper() in {"ACTIVE", "PAUSED"}
            for config in [reconciliation_config_for_deployment(deployment)]
            if config.enabled
        ]
        if not intervals:
            return DEFAULT_RECONCILIATION_INTERVAL_SECONDS
        return min(intervals)


async def run_broker_reconciliation_loop(
    active_adapters: Mapping[str, BaseLiveAdapter],
    current_deployments: Callable[[], Iterable[LiveDeploymentConfig]],
    *,
    manager: BrokerReconciliationManager | None = None,
    deployments_ready: asyncio.Event | None = None,
) -> None:
    """Scheduled pass driver; interval comes from deployment settings (#303).

    ``deployments_ready`` gates the first pass on the daemon's initial
    deployment sync: reconciling an empty snapshot would write an empty report
    and wait a full interval before the real accounts are ever checked.
    """
    manager = manager or BrokerReconciliationManager()
    if deployments_ready is not None:
        await deployments_ready.wait()
    while True:
        try:
            deployments = list(current_deployments())
            await manager.reconcile_once(deployments, active_adapters)
            interval = manager.next_interval_seconds(deployments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: loop-survival
            logger.warning(
                "Broker reconciliation iteration failed",
                extra={"data": {"error": str(exc)}},
            )
            interval = DEFAULT_RECONCILIATION_INTERVAL_SECONDS
        await asyncio.sleep(interval)


def load_reconciliation_report(path: Path | None = None) -> dict[str, Any]:
    """Read the latest report for the read-only API; absent file is an empty report."""
    report_path = path or default_reconciliation_report_path()
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"generated_at": None, "accounts": {}}
    except (OSError, json.JSONDecodeError) as exc:
        return {"generated_at": None, "accounts": {}, "error": str(exc)}
    return payload if isinstance(payload, dict) else {"generated_at": None, "accounts": {}}


def refresh_deployments_from_db() -> list[LiveDeploymentConfig]:
    """Helper for tests/tools that need the same deployment list the daemon uses."""
    return list_live_deployments()
