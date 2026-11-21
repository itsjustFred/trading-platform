"""Periodic broker reconciliation for live deployments (#303).

The ledger is the local truth for what a deployment owns; the broker is the
truth for what is actually open. This module compares, per account (broker
positions and working orders are account-level), the two views on a schedule:

* signed ledger quantity per symbol vs the broker's net position;
* broker order ids the ledger still expects to be working vs the broker's
  working orders (position-protection legs are marked by the adapter and are
  not treated as untracked entries/exits);
* protective stops vs open broker positions, for adapters that can list them.

Unreadable broker state is a mismatch (fail closed), but never flaps: a pause
requires ``consecutive_mismatches`` (default 2) consecutive bad passes, and a
clean pass resets the streak. The pause is the ordinary deployment status
transition to ``PAUSED`` - the same control an operator uses - so the existing
dispatch claim, submission drain and STOPPED enforcement keep their semantics.
The latest pass is written to a JSON report the viewer serves read-only.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_runner.base_adapter import BaseLiveAdapter, BrokerOrderView, ProtectiveStop
from live_runner.exit_signals import DEFAULT_QTY_TOLERANCE, usable_qty_tolerance
from shared import constants as shared_constants
from shared.database import (
    get_account_net_filled_qty_by_deployment,
    list_live_account_filled_symbols,
    list_live_account_order_expectations,
    list_live_deployments,
    update_live_deployment_status,
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


def default_reconciliation_report_path() -> Path:
    """Where the daemon writes the latest read-only reconciliation report."""
    return shared_constants.DATA_DIR / "live" / RECONCILIATION_REPORT_FILENAME


@dataclass(frozen=True, slots=True)
class BrokerReconciliationConfig:
    """Effective per-account reconciliation settings (#303).

    ``qty_tolerance`` of ``None`` means "ask the adapter for the instrument's
    precision" (with the strict default when it cannot say). ``interval_seconds``
    is the sleep between passes; the loop uses the smallest configured interval
    across accounts so no account waits longer than it asked.
    """

    interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS
    qty_tolerance: float | None = None
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
    paused_deployment_ids: tuple[str, ...] = ()

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
            "paused_deployment_ids": list(self.paused_deployment_ids),
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


PauseDeployment = Callable[[str], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _setting_bool(settings: Mapping[str, Any], key: str, *, default: bool) -> bool:
    raw = settings.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(
    settings: Mapping[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = settings.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be in [{minimum}, {maximum}], got {value}")
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
        consecutive_mismatches=consecutive,
        enabled=_setting_bool(settings, "broker_reconciliation_enabled", default=True),
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
    adapter: BaseLiveAdapter, symbol: str, configured: float | None
) -> float:
    if configured is not None:
        return usable_qty_tolerance(configured)
    method = getattr(adapter, "position_qty_tolerance", None)
    raw = DEFAULT_QTY_TOLERANCE
    if callable(method):
        try:
            raw = method(symbol)
        except Exception as exc:
            logger.warning(
                "Adapter could not report quantity precision; using strict default",
                extra={"data": {"symbol": symbol, "error": str(exc)}},
            )
    return usable_qty_tolerance(raw)


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


async def _broker_state(adapter: BaseLiveAdapter) -> BrokerStateSnapshot:
    """Read positions, working orders and protective stops; any failure is unreadable."""
    try:
        positions = tuple(await adapter.get_positions())
    except Exception as exc:
        return BrokerStateSnapshot(error=f"positions unreadable: {exc}")
    try:
        orders = tuple(await adapter.list_reconciliation_orders())
    except NotImplementedError as exc:
        return BrokerStateSnapshot(error=f"working orders unreadable: {exc}")
    except Exception as exc:
        return BrokerStateSnapshot(error=f"working orders unreadable: {exc}")

    stops: tuple[ProtectiveStop, ...] = ()
    stops_supported = False
    list_stops = getattr(adapter, "list_protective_stops", None)
    if callable(list_stops):
        try:
            stops = tuple(await list_stops())
            stops_supported = True
        except NotImplementedError:
            stops_supported = False
        except Exception as exc:
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
    rows: Iterable[Mapping[str, Any]], tolerance_for: Callable[[str], float]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Signals the ledger still expects to be working, and tracked broker ids."""
    expected: list[dict[str, Any]] = []
    tracked_ids: set[str] = set()
    for row in rows:
        signal_id = str(row.get("signal_id") or "").strip()
        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if signal_id:
            tracked_ids.add(signal_id)
        if not broker_order_id:
            continue
        tracked_ids.add(broker_order_id)
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
        return _position_qty_tolerance(adapter, symbol, config.qty_tolerance)

    expected_orders, tracked_ids = _order_expectations(order_rows, tolerance_for)
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
        if not (ids & broker_non_protection_ids):
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
        for symbol in sorted(position_symbols - stop_symbols):
            findings.append(
                _finding(
                    "position_without_protective_stop",
                    account_id=account_id,
                    symbol=symbol,
                    broker_qty=broker_positions[symbol],
                )
            )
        for symbol in sorted(stop_symbols - position_symbols):
            findings.append(
                _finding(
                    "orphaned_protective_stop",
                    account_id=account_id,
                    symbol=symbol,
                )
            )

    return findings, summary


async def _default_pause_deployment(deployment_id: str) -> None:
    await asyncio.to_thread(update_live_deployment_status, deployment_id, "PAUSED")


class BrokerReconciliationManager:
    """Holds consecutive-mismatch streaks and the latest report per account."""

    def __init__(
        self,
        *,
        report_path: Path | None = None,
        pause_deployment: PauseDeployment | None = None,
    ) -> None:
        self._streaks: dict[str, int] = {}
        self._reports: dict[str, AccountReconciliationReport] = {}
        self._report_path = report_path or default_reconciliation_report_path()
        self._pause_deployment = pause_deployment or _default_pause_deployment

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

    async def reconcile_account(
        self,
        *,
        account_id: str,
        deployments: list[LiveDeploymentConfig],
        adapter: BaseLiveAdapter | None,
        config: BrokerReconciliationConfig,
    ) -> AccountReconciliationReport:
        generated_at = _utcnow()
        if adapter is None:
            findings = [
                _finding("broker_state_unreadable", account_id=account_id, error="no adapter")
            ]
            summary: dict[str, Any] = {}
        else:
            snapshot = await _broker_state(adapter)
            findings, summary = await asyncio.to_thread(
                compare_account_state,
                account_id=account_id,
                snapshot=snapshot,
                deployments=deployments,
                config=config,
                adapter=adapter,
            )

        mismatched = bool(findings)
        streak = self._streaks.get(account_id, 0) + 1 if mismatched else 0
        self._streaks[account_id] = streak

        paused: list[str] = []
        active = self._active_deployments(deployments)
        should_pause = mismatched and streak >= config.consecutive_mismatches and bool(active)
        if should_pause:
            for deployment in active:
                deployment_id = str(deployment.id)
                try:
                    await self._pause_deployment(deployment_id)
                    paused.append(deployment_id)
                except Exception as exc:
                    logger.error(
                        "Broker reconciliation could not pause deployment",
                        extra={
                            "data": {
                                "alert": "broker_reconciliation_pause_failed",
                                "account_id": account_id,
                                "deployment_id": deployment_id,
                                "error": str(exc),
                            }
                        },
                    )
                    findings.append(
                        _finding(
                            "pause_failed",
                            account_id=account_id,
                            deployment_id=deployment_id,
                            error=str(exc),
                        )
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
                        "paused_deployment_ids": paused,
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
            ok=not mismatched,
            unreadable=any(f.kind == "broker_state_unreadable" for f in findings),
            findings=tuple(findings),
            consecutive_mismatches=streak,
            required_mismatches=config.consecutive_mismatches,
            paused_deployment_ids=tuple(paused),
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
) -> None:
    """Scheduled pass driver; interval comes from deployment settings (#303)."""
    manager = manager or BrokerReconciliationManager()
    while True:
        try:
            deployments = list(current_deployments())
            await manager.reconcile_once(deployments, active_adapters)
            interval = manager.next_interval_seconds(deployments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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
