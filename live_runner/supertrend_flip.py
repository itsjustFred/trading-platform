"""Live ``supertrend_flip`` workflow (issue #168).

The live half of the replay-only workflow in
``runner/engines/signal_replay_adapter.py``: TP1 partial close, breakeven stop
amendment, then exit of the remainder on the first red Supertrend bar after
the first green one. Alpaca stays replay-only until #350's paper run passes;
the per-deployment opt-in that lets a cTrader deployment ask for this mode is
wired separately (daemon side, default off).

Design, mirroring ``live_runner/trailing_stops.py`` where the guarantees are
the same:

* **Broker state is the truth.** Position quantity, entry price, position id
  and the current protective stop are re-read from the broker on every pass.
  The workflow's own progress lives in the *entry signal's* persisted metadata
  (``metadata["supertrend_flip_state"]``), so a restarted daemon resumes
  exactly where it stopped - and the deterministic exit-signal ids below make
  even a crash between "order submitted" and "state persisted" recoverable:
  resubmitting the same ``LiveSignal`` id returns the already-persisted broker
  order id instead of placing a second order.
* **TP1 is a native partial close.** ``ProtoOAClosePositionReq`` against the
  broker position id, sized ``tp1_partial_quantity`` of the open quantity -
  the same whole-unit floor the replay applies. The tranche's accounting is
  the executions ledger's job (the exit signal is a normal ``exit_only`` live
  signal), exactly like every other partial close.
* **TP1 is confirmed by the broker, not by the request.** The phase only
  advances once the open quantity actually dropped by the tranche; a working
  order is waited on, a terminal one is retried under a new deterministic id,
  and anything ambiguous is an outage that alerts and backs off - never a
  silent second close.
* **Breakeven waits for the TP1 bar to close.** Same dead-bar rule as replay:
  nothing at or inside the TP1 bar moves the trail, and the Supertrend
  evaluation starts on the bar after it. The amendment itself goes through
  ``adapter.amend_protective_stop`` (in-place, broker-confirmed, previous stop
  preserved on any ambiguity) under the shutdown mutation gate.
* **The flip exit is a market close of the remainder**, submitted when a
  freshly closed bar flips red. Replay fills at the next bar's open; live
  detects the closed bar on the next tick (<= ``FLIP_TICK_SECONDS`` into that
  bar) and closes at market, which is the same price region.
* **Fail closed.** A malformed persisted state, an unparseable plan, a
  position that cannot be attributed to exactly one entry, an adapter that
  cannot serve the deployment's timeframe, or a broker read that keeps
  failing all surface as errors; permanent ones halt the workflow for that
  position (broker stop stays in force), transient ones retry with backoff
  and give up loudly after ``FLIP_GIVE_UP_AFTER_SECONDS``.

Scope limits (deliberate):

* Long entries only, as ``parse_supertrend_flip_plan`` requires.
* ACTIVE and PAUSED deployments are managed: every action the workflow takes
  is a close or a protective amendment of an already-open position, which is
  what a pause explicitly still allows (#126). STOPPED deployments are left
  to the flatten enforcement.
* A position is managed only when the newest filled entry signal for its
  symbol carries ``trailing_sl_config.mode == "supertrend_flip"``. Anything
  else belongs to the percent-trailing loop or to nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from live_runner.account_order_lock import account_order_placement
from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    ORDER_STATE_FILLED,
    ORDER_STATE_TERMINAL,
    ORDER_STATE_WORKING,
    BaseLiveAdapter,
    ProtectiveStop,
    validate_live_quote_prices,
)
from live_runner.exit_signals import ExitBelowStepError
from live_runner.shutdown import BrokerMutationGate
from live_runner.signal_sources.execution_timing import _timeframe_seconds
from live_runner.trailing_stops import (
    TRAILING_GIVE_UP_AFTER_SECONDS,
    TRAILING_GIVEN_UP_PROBE_SECONDS,
    TRAILING_RETRY_INITIAL_SECONDS,
    TRAILING_RETRY_MAX_SECONDS,
    _Outage,
)
from shared.database import (
    find_latest_filled_live_entry_signals,
    get_live_signal_filled_qty,
    get_live_signal_metadata,
    merge_live_signal_metadata,
)
from shared.logging import get_logger
from shared.lot_grid import floor_qty_to_step
from shared.models import LiveDeploymentConfig, LiveSignal
from shared.sentry import capture_exception
from shared.trailing import (
    FLIP_DEPLOYMENT_OPT_IN_KEY,
    TRAILING_MODE_SUPERTREND_FLIP,
    TRAILING_SL_CONFIG_METADATA_KEY,
    SupertrendFlipPlan,
    deployment_supertrend_flip_enabled,
    normalize_trailing_mode,
    parse_supertrend_flip_plan,
    tp1_partial_quantity,
)
from signal_bots.ramjet.processor import calculate_supertrend

logger = get_logger("live-runner")

#: Key under the entry signal's ``metadata_json`` holding the workflow state.
FLIP_STATE_METADATA_KEY = "supertrend_flip_state"

#: Workflow phases persisted in the state. ``tp1_pending``/``exit_pending`` are
#: live-only transport states: the order is submitted and the phase advances
#: once the broker shows the result.
PHASE_BEFORE_TP1 = "before_tp1"
PHASE_TP1_PENDING = "tp1_pending"
PHASE_AWAIT_GREEN = "await_green"
PHASE_ACTIVE = "active"
PHASE_EXIT_PENDING = "exit_pending"
PHASE_HALTED = "halted"
#: Terminal: the bound broker position is gone (stopped out, closed, or never
#: ours). Nothing may manage it again - a later position on the same symbol is
#: a different ``position_id`` and is refused by the binding check.
PHASE_DONE = "done"
_KNOWN_PHASES = frozenset(
    {
        PHASE_BEFORE_TP1,
        PHASE_TP1_PENDING,
        PHASE_AWAIT_GREEN,
        PHASE_ACTIVE,
        PHASE_EXIT_PENDING,
        PHASE_HALTED,
        PHASE_DONE,
    }
)
_TP1_PHASES = frozenset({PHASE_BEFORE_TP1, PHASE_TP1_PENDING})
_POST_TP1_PHASES = frozenset({PHASE_AWAIT_GREEN, PHASE_ACTIVE, PHASE_EXIT_PENDING})

#: How often the daemon loop should wake this manager. TP1 is an intrabar
#: trigger, so - unlike the bar-boundary trailing loop - a healthy pass runs
#: every tick; the tick only bounds quote-check latency.
FLIP_TICK_SECONDS = 15.0
#: Bars requested per Supertrend evaluation. Must cover the warm-up
#: (``supertrend_period``) plus enough history for the Wilder-RMA bands to
#: converge to the replay's full-history values; ``period * 30`` keeps the
#: residual seed dependence negligible. Bounded by the adapter's own limit.
FLIP_BAR_LOOKBACK = 300
#: A submitted TP1/remainder close that is still unresolved after this long is
#: a failure, not patience: market closes fill in seconds, so a working order
#: this old means something is wrong and the position needs an operator.
FLIP_PENDING_TIMEOUT_SECONDS = 15 * 60
#: Retries of one logical close before the workflow halts. Each retry uses a
#: new deterministic signal id, so a terminal (rejected/cancelled) order can
#: never be double-submitted under the same id.
FLIP_MAX_CLOSE_ATTEMPTS = 8
#: Account-level backoff bounds, same policy as the trailing loop.
FLIP_RETRY_INITIAL_SECONDS = TRAILING_RETRY_INITIAL_SECONDS
FLIP_RETRY_MAX_SECONDS = TRAILING_RETRY_MAX_SECONDS
FLIP_GIVE_UP_AFTER_SECONDS = TRAILING_GIVE_UP_AFTER_SECONDS
FLIP_GIVEN_UP_PROBE_SECONDS = TRAILING_GIVEN_UP_PROBE_SECONDS

#: Deterministic namespace for the exit signals the workflow submits. The ids
#: are derived from the entry signal id (plus the flip event and the attempt
#: number), so a retry after a crash lands on the same ``LiveSignal`` row and
#: ``submit_order``'s persisted-order-id recovery answers instead of placing a
#: second broker order.
_FLIP_SIGNAL_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://trading-platform.local/live-supertrend-flip"
)

EntrySignalLoader = Callable[[list[str], list[str]], dict[str, dict[str, Any]]]

#: Deployment ``settings`` key opting a deployment into the live
#: ``supertrend_flip`` workflow (#168). Canonical definition lives in
#: ``shared.trailing`` (deployment validation reads it there); re-exported
#: here for the live runner's callers.
FLIP_DEPLOYMENT_OPT_IN_KEY = FLIP_DEPLOYMENT_OPT_IN_KEY

#: ``(account_id, symbol) -> asyncio.Lock`` factory serialising exits, injected
#: by the daemon (``live_runner.daemon._exit_lock``) so flip closes cannot
#: interleave with a Discord/internal exit sized on the same position.
ExitLockFactory = Callable[[str, str], Any]

#: ``(adapter, deployment, symbol) -> list[Mapping]`` in-flight exit check,
#: injected by the daemon (``_ensure_no_exit_in_flight``). It raises on this
#: deployment's own working exit and returns other deployments' working exits;
#: the flip manager treats both as "defer this close".
ExitInFlightCheck = Callable[
    [BaseLiveAdapter, LiveDeploymentConfig, str], Any
]

#: Broker names the workflow may run against today. cTrader only: it is the
#: adapter with native partial closes and in-place stop amendment wired for
#: this workflow. Alpaca stays out until #350's paper run passes, so an
#: opted-in Alpaca deployment is simply never managed.
FLIP_SUPPORTED_BROKERS = frozenset({"ctrader"})


def _flip_signal_id(
    kind: str, entry_signal_id: str, *, event: str = "", attempt: int = 0
) -> uuid.UUID:
    """The deterministic ``LiveSignal.id`` for one workflow close attempt."""
    return uuid.uuid5(
        _FLIP_SIGNAL_NAMESPACE, f"{kind}:{entry_signal_id}:{event}:{attempt}"
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class _FlipFailure(Exception):
    """One position's pass could not complete; surfaced and retried.

    ``permanent`` marks a failure no retry can fix (a malformed plan, a state
    that cannot be trusted, a capability the adapter lacks): the workflow
    halts for that position and the broker-side stop stays in force.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class _FlipPositionMismatch(Exception):
    """The broker position is not the one the persisted plan is bound to.

    Carries the entry and state so the caller can persist ``done`` and alert
    without ever touching the foreign position.
    """

    def __init__(
        self,
        message: str,
        *,
        entry: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.entry = entry
        self.state = state


class _FlipDeferredClose(Exception):
    """Another exit is in flight for this position; defer, do not fail."""


class _FlipShutdownRefused(Exception):
    """The shutdown gate refused the mutation; the pass simply ends."""


@dataclass
class _Workflow:
    """In-memory progress of one managed position. Safe to lose.

    Everything that must survive a restart is in the entry signal's metadata;
    this only carries the failure/backoff bookkeeping so an unhealthy pass is
    not retried every tick.
    """

    signal_id: str
    outage: _Outage = field(default_factory=_Outage)
    retry_at: datetime | None = None
    retry_delay: float = FLIP_RETRY_INITIAL_SECONDS


@dataclass
class _AccountSchedule:
    retry_at: datetime | None = None
    retry_delay: float = FLIP_RETRY_INITIAL_SECONDS
    #: Failures of the account-wide stop listing, counted once per account.
    stops_outage: _Outage = field(default_factory=_Outage)


@dataclass
class _ProtectiveStopListing:
    """The account-wide stop listing shared by one pass's due positions.

    ``stops`` stays ``None`` until the first due position triggers the read;
    ``unavailable`` marks a failed read so later positions do not retry it
    within the same pass (the failure itself is paced on the account
    schedule).
    """

    stops: list[ProtectiveStop] | None = None
    unavailable: bool = False


def _bar_open_utc(raw_time: Any) -> datetime:
    """A bar row's timestamp as a UTC ``datetime``."""
    opened = pd.Timestamp(raw_time)
    if opened.tzinfo is None:
        opened = opened.tz_localize("UTC")
    else:
        opened = opened.tz_convert("UTC")
    return opened.to_pydatetime()


def _bar_decidable(
    open_time: datetime,
    *,
    moment: datetime,
    bar_seconds: int,
    after: datetime | None,
    dead_at_or_before: datetime | None,
) -> bool:
    """Whether this bar's close is final and the workflow may act on it.

    A bar counts once ``open + length <= now``; bars at or before ``after``
    were already evaluated and bars at or before ``dead_at_or_before`` (the
    TP1 bar) are dead for the workflow by rule, not by accident.
    """
    if open_time + timedelta(seconds=bar_seconds) > moment:
        return False
    if after is not None and open_time <= after:
        return False
    return not (
        dead_at_or_before is not None and open_time <= dead_at_or_before
    )


def _normalize_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _fresh_state(position_qty: float, entry_price: float) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": PHASE_BEFORE_TP1,
        "entry_qty": abs(float(position_qty)),
        "entry_price": float(entry_price) if math.isfinite(entry_price) else 0.0,
        "tp1_time": None,
        "tp1_qty": None,
        "tp1_trigger_time": None,
        "tp1_bar_open": None,
        "tp1_skipped_time": None,
        "tp1_order_id": None,
        "tp1_signal_id": None,
        "tp1_attempts": 0,
        "tp1_pending_since": None,
        "activation_time": None,
        "flip_bar_time": None,
        "last_bar_time": None,
        "exit_order_id": None,
        "exit_signal_id": None,
        "exit_qty": None,
        "exit_attempts": 0,
        "exit_pending_since": None,
        "position_id": None,
        "tp1_filled_qty": None,
        "breakeven_done": False,
        "halt_reason": None,
    }


def _normalize_state(raw: Any, *, context: str) -> dict[str, Any] | None:
    """The persisted workflow state, or ``None`` when none was ever written.

    Anything that is present but not a state this code wrote is ambiguous -
    acting on a guess could double-close - so it raises a permanent failure.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _FlipFailure(
            f"supertrend_flip state for {context} is not a mapping "
            f"({type(raw).__name__}); refusing to guess workflow progress",
            permanent=True,
        )
    phase = raw.get("phase")
    if phase not in _KNOWN_PHASES:
        raise _FlipFailure(
            f"supertrend_flip state for {context} has unknown phase {phase!r}; "
            "refusing to guess workflow progress",
            permanent=True,
        )
    state = _fresh_state(0.0, 0.0)
    state.update(raw)
    state["phase"] = phase
    # Coerce the numeric fields the machine reads; a value that cannot be read
    # is corrupt state, not a default.
    for key in ("entry_qty", "tp1_qty", "exit_qty", "tp1_filled_qty"):
        if state.get(key) is not None:
            number = _float_or_none(state.get(key))
            if number is None or number < 0:
                raise _FlipFailure(
                    f"supertrend_flip state for {context} has unreadable {key} "
                    f"({state.get(key)!r})",
                    permanent=True,
                )
            state[key] = number
    if state.get("position_id") is not None:
        try:
            state["position_id"] = int(state["position_id"])
        except (TypeError, ValueError) as exc:
            raise _FlipFailure(
                f"supertrend_flip state for {context} has unreadable "
                f"position_id ({state.get('position_id')!r})",
                permanent=True,
            ) from exc
    for key in ("tp1_attempts", "exit_attempts"):
        try:
            state[key] = max(0, int(state.get(key) or 0))
        except (TypeError, ValueError) as exc:
            raise _FlipFailure(
                f"supertrend_flip state for {context} has unreadable {key}",
                permanent=True,
            ) from exc
    state["breakeven_done"] = state.get("breakeven_done") is True
    return state


class LiveSupertrendFlipManager:
    """Runs the ``supertrend_flip`` workflow for open positions."""

    def __init__(
        self,
        *,
        load_entry_signals: EntrySignalLoader = find_latest_filled_live_entry_signals,
        mutation_gate: BrokerMutationGate | None = None,
        exit_lock: ExitLockFactory | None = None,
        exit_in_flight_check: ExitInFlightCheck | None = None,
        state_key: str = FLIP_STATE_METADATA_KEY,
    ) -> None:
        self._load_entry_signals = load_entry_signals
        self._mutation_gate = mutation_gate
        self._exit_lock = exit_lock
        self._exit_in_flight_check = exit_in_flight_check
        self._state_key = state_key
        self._workflows: dict[tuple[str, str], _Workflow] = {}
        self._schedules: dict[str, _AccountSchedule] = {}

    # ── introspection (tests, diagnostics) ───────────────────────────────

    def tracked_positions(self) -> set[tuple[str, str]]:
        """``(account_id, SYMBOL)`` pairs with in-memory workflow progress."""
        return set(self._workflows)

    # ── scheduling ───────────────────────────────────────────────────────

    async def run_once(
        self,
        adapters: Mapping[str, BaseLiveAdapter],
        deployments: Iterable[LiveDeploymentConfig],
        *,
        now: datetime | None = None,
    ) -> None:
        """One pass over every account that could hold a flip position.

        Unlike the trailing loop there is no bar-boundary gate: TP1 is an
        intrabar trigger, so every tick visits each eligible account. An
        account-level failure (positions or entry signals unreadable) backs
        off through ``_AccountSchedule``; per-position failures pace
        themselves through ``_Workflow.retry_at``.
        """
        moment = now or datetime.now(timezone.utc)
        managed_by_account = self._managed_deployments_by_account(deployments)
        eligible = self._eligible_flip_adapters(adapters, managed_by_account)
        gone = (set(self._schedules) | {key[0] for key in self._workflows}) - set(
            eligible
        )
        for account_id in gone:
            self._forget_account(account_id)

        for account_id, adapter in eligible.items():
            schedule = self._schedules.setdefault(account_id, _AccountSchedule())
            if schedule.retry_at is not None and moment < schedule.retry_at:
                continue
            try:
                await self._manage_account(
                    account_id, adapter, managed_by_account[account_id], moment
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # except-kind: money-decision
                self._surface(
                    "Live supertrend-flip pass failed for account; broker-side "
                    "stops are unchanged",
                    exc,
                    {"account_id": account_id, "adapter": type(adapter).__name__},
                )
                schedule.retry_at = moment + timedelta(seconds=schedule.retry_delay)
                schedule.retry_delay = min(
                    schedule.retry_delay * 2.0, FLIP_RETRY_MAX_SECONDS
                )
            else:
                schedule.retry_at = None
                schedule.retry_delay = FLIP_RETRY_INITIAL_SECONDS

    @staticmethod
    def _managed_deployments_by_account(
        deployments: Iterable[LiveDeploymentConfig],
    ) -> dict[str, list[LiveDeploymentConfig]]:
        managed: dict[str, list[LiveDeploymentConfig]] = {}
        for deployment in deployments:
            status = str(deployment.status or "").strip().upper()
            # PAUSED still manages its open positions: every workflow action is
            # a close or a protective amendment, which a pause allows (#126).
            # The settings opt-in is the other half of "explicit": without it
            # the deployment's flip signals are nobody's workflow.
            if status in {"ACTIVE", "PAUSED"} and deployment_supertrend_flip_enabled(
                deployment
            ):
                managed.setdefault(deployment.account_id, []).append(deployment)
        return managed

    @staticmethod
    def _eligible_flip_adapters(
        adapters: Mapping[str, BaseLiveAdapter],
        managed_by_account: Mapping[str, list[LiveDeploymentConfig]],
    ) -> dict[str, BaseLiveAdapter]:
        return {
            account_id: adapter
            for account_id, adapter in adapters.items()
            if account_id in managed_by_account
            and getattr(adapter, "broker_name", "") in FLIP_SUPPORTED_BROKERS
            and getattr(adapter, "supports_supertrend_flip", False) is True
        }

    def _forget_account(self, account_id: str) -> None:
        self._schedules.pop(account_id, None)
        for key in [key for key in self._workflows if key[0] == account_id]:
            del self._workflows[key]

    # ── one account ──────────────────────────────────────────────────────

    async def _manage_account(
        self,
        account_id: str,
        adapter: BaseLiveAdapter,
        deployments: list[LiveDeploymentConfig],
        moment: datetime,
    ) -> None:
        positions = await self._read_positions(adapter)
        for key in [
            k for k in self._workflows if k[0] == account_id and k[1] not in positions
        ]:
            workflow = self._workflows.pop(key)
            await self._mark_done_by_signal_id(workflow.signal_id)
        if not positions:
            return

        deployments_by_id = {str(item.id): item for item in deployments}
        entry_signals = await asyncio.to_thread(
            self._load_entry_signals, sorted(deployments_by_id), sorted(positions)
        )

        stops = _ProtectiveStopListing()
        for symbol, rows in positions.items():
            await self._manage_symbol(
                account_id=account_id,
                adapter=adapter,
                deployments_by_id=deployments_by_id,
                entry=entry_signals.get(symbol),
                symbol=symbol,
                rows=rows,
                stops=stops,
                moment=moment,
            )

    async def _manage_symbol(
        self,
        *,
        account_id: str,
        adapter: BaseLiveAdapter,
        deployments_by_id: Mapping[str, LiveDeploymentConfig],
        entry: Mapping[str, Any] | None,
        symbol: str,
        rows: list[Mapping[str, Any]],
        stops: _ProtectiveStopListing,
        moment: datetime,
    ) -> None:
        """One position's pass; failures are recorded, never propagated."""
        context = {
            "account_id": account_id,
            "symbol": symbol,
            "adapter": type(adapter).__name__,
            "signal_id": (entry or {}).get("signal_id"),
            "deployment_id": (entry or {}).get("deployment_id"),
        }
        key = (account_id, symbol)
        try:
            prepared = self._prepare_position(
                adapter, deployments_by_id, entry, rows, context
            )
            if prepared is None:
                self._workflows.pop(key, None)
                return
            deployment, plan, state, position = prepared
            if entry is None:
                # ``_prepare_position`` only returns a plan for a real entry;
                # reaching this means the loader contract broke. Fail closed
                # rather than assert inside the money-decision handler below.
                raise _FlipFailure(
                    f"prepared flip position for {symbol} lost its entry "
                    "signal; refusing to manage it",
                    permanent=True,
                )
            workflow = self._workflow_for(key, str(entry["signal_id"]))
            if workflow.outage.probe_blocked(moment) or (
                workflow.retry_at is not None and moment < workflow.retry_at
            ):
                return
            if state.get("_verify_entry_qty") and not (
                await self._position_proven_for_entry(adapter, position, entry)
            ):
                # First sight (#168): adopt ONLY a position the broker proves
                # this entry's order opened. A foreign position that merely
                # matches the ledgered quantity - a manual trade, another
                # deployment's entry, or a later long after the own position
                # already closed unseen - is never managed: persist ``done``
                # so a restart cannot adopt it later, alert once, take no
                # action.
                clean_state = {
                    key: value
                    for key, value in state.items()
                    if not str(key).startswith("_")
                }
                await self._mark_position_done(entry, clean_state)
                self._surface(
                    "Live supertrend-flip first sight could not prove the "
                    "broker position was opened by the entry's order; "
                    "marking done and taking no action on it",
                    _FlipFailure(
                        "position provenance unproven at first sight",
                        permanent=True,
                    ),
                    context,
                    critical=True,
                )
                self._workflows.pop(key, None)
                return
            bar_seconds = _timeframe_seconds(deployment.signal_timeframe) or 0
            breakeven_due = self._breakeven_due(plan, state, bar_seconds, moment)
            stops_for_position: list[ProtectiveStop] | None = None
            if breakeven_due:
                await self._load_stops_once(
                    adapter, account_id, stops, context, moment
                )
                if stops.stops is None:
                    # A failed stop listing defers ONLY the breakeven amend -
                    # it must never block TP1 or the flip exit: the workflow
                    # continues with ``stops=None`` and the amend is retried
                    # once listing recovers. ``_record_stops_failure`` has
                    # already surfaced the outage (critical on give-up).
                    logger.warning(
                        "Live supertrend-flip breakeven amend deferred: "
                        "protective-stop listing unavailable; the flip "
                        "workflow continues unmanaged-stop aware",
                        extra={"data": context},
                    )
                else:
                    stops_for_position = stops.stops
            await self._manage_position(
                adapter=adapter,
                deployment=deployment,
                entry=entry,
                position=position,
                plan=plan,
                state=state,
                stops=stops_for_position,
                bar_seconds=bar_seconds,
                moment=moment,
                context=context,
            )
            self._record_success(workflow)
        except _FlipPositionMismatch as exc:
            # The broker position is not the one the plan is bound to: alert,
            # persist ``done`` so a restart cannot adopt it later, and take
            # NO trade action on it.
            await self._mark_position_done(exc.entry, exc.state)
            self._surface(
                "Live supertrend-flip position identity mismatch; no action "
                "taken on the foreign position",
                exc,
                context,
                critical=True,
            )
            self._workflows.pop(key, None)
        except (_FlipDeferredClose, _FlipShutdownRefused):
            # Another exit is working or the daemon is stopping: the intent
            # was persisted before the submit attempt, so the next pass
            # resumes cleanly. Not a failure.
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: money-decision
            await self._record_failure(key, entry, exc, context, moment)

    async def _load_stops_once(
        self,
        adapter: BaseLiveAdapter,
        account_id: str,
        stops: _ProtectiveStopListing,
        context: Mapping[str, Any],
        moment: datetime,
    ) -> None:
        """The shared account-wide stop listing, read lazily once per pass.

        One read serves every due position; a failure is tracked on the
        account schedule, not per position.
        """
        if stops.stops is not None or stops.unavailable:
            return
        try:
            stops.stops = list(await adapter.list_protective_stops())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: money-decision
            stops.unavailable = True
            self._record_stops_failure(account_id, exc, context, moment)

    async def _read_positions(
        self, adapter: BaseLiveAdapter
    ) -> dict[str, list[Mapping[str, Any]]]:
        get_positions = getattr(adapter, "get_positions", None)
        if not callable(get_positions):
            raise _FlipFailure(
                f"{type(adapter).__name__} cannot report positions", permanent=True
            )
        positions: dict[str, list[Mapping[str, Any]]] = {}
        for row in await get_positions():
            symbol = _normalize_symbol(row.get("symbol"))
            if symbol:
                positions.setdefault(symbol, []).append(row)
        return positions

    @staticmethod
    def _position_qty(position: Mapping[str, Any]) -> float:
        if position.get("qty_unparseable"):
            raise _FlipFailure(
                f"broker position qty is unparseable ({position.get('qty_raw')!r})"
            )
        try:
            qty = float(position.get("qty") or 0.0)
        except (TypeError, ValueError) as exc:
            raise _FlipFailure(
                f"broker position qty is unparseable ({position.get('qty')!r})"
            ) from exc
        if not math.isfinite(qty):
            raise _FlipFailure(f"broker position qty is not finite ({qty!r})")
        return qty

    def _prepare_position(
        self,
        adapter: BaseLiveAdapter,
        deployments_by_id: Mapping[str, LiveDeploymentConfig],
        entry: Mapping[str, Any] | None,
        rows: list[Mapping[str, Any]],
        context: Mapping[str, Any],
    ) -> (
        tuple[LiveDeploymentConfig, SupertrendFlipPlan, dict[str, Any], Mapping[str, Any]]
        | None
    ):
        """Everything needed to manage one position, or ``None`` to skip it.

        ``None`` means "not ours": no filled entry signal, a non-flip trailing
        config, a short/flat position, or a deployment that is no longer
        managed. Anything ambiguous raises instead.
        """
        if entry is None:
            return None
        metadata = entry.get("metadata") or {}
        trailing_cfg = metadata.get(TRAILING_SL_CONFIG_METADATA_KEY)
        if (
            not isinstance(trailing_cfg, Mapping)
            or normalize_trailing_mode(trailing_cfg.get("mode"))
            != TRAILING_MODE_SUPERTREND_FLIP
        ):
            return None
        if len(rows) != 1:
            raise _FlipFailure(
                f"expected exactly one broker position row for "
                f"{context.get('symbol')}, found {len(rows)}; cannot attribute "
                "the flip workflow",
                permanent=True,
            )
        position = rows[0]
        qty = self._position_qty(position)
        if qty <= 0:
            return None
        deployment = deployments_by_id.get(str(entry.get("deployment_id") or ""))
        if deployment is None:
            return None
        timeframe = deployment.signal_timeframe
        if not _timeframe_seconds(timeframe):
            raise _FlipFailure(
                f"deployment timeframe {timeframe!r} has no bar length for the "
                "flip workflow",
                permanent=True,
            )
        self._require_flip_market_data(adapter, timeframe)
        symbol = _normalize_symbol(position.get("symbol"))
        try:
            plan = parse_supertrend_flip_plan(
                dict(trailing_cfg),
                metadata,
                action=str(entry.get("action") or ""),
                take_profit=_float_or_none(metadata.get("take_profit")),
                stop_loss=_float_or_none(metadata.get("stop_loss")),
                context=(
                    f"live supertrend_flip for {symbol} "
                    f"(signal {entry.get('signal_id')})"
                ),
            )
        except ValueError as exc:
            raise _FlipFailure(str(exc), permanent=True) from exc
        try:
            entry_price = float(position.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            entry_price = 0.0
        state = _normalize_state(
            metadata.get(self._state_key),
            context=f"{symbol} (signal {entry.get('signal_id')})",
        )
        if state is not None and state.get("phase") in {PHASE_DONE, PHASE_HALTED}:
            # A finished plan is never ours again: after ``done`` the bound
            # position is expected to be gone, so ANY later position on the
            # symbol - the normal case once the flip trade ended and
            # someone else opened a long - must not raise the identity
            # mismatch below every pass. Returning ``None`` ("not ours")
            # alerts only on the transition that set ``done``.
            return None
        position_id = self._position_id(position)
        if state is None:
            state = _fresh_state(qty, entry_price)
            # First sight of this position: bind the plan to THIS broker
            # position id and to the entry's ledgered fill quantity. The
            # ``_``-prefixed markers are consumed (and stripped) by
            # ``_persist_state`` on the first managed pass.
            state["position_id"] = position_id
            state["_needs_persist"] = True
            state["_verify_entry_qty"] = True
        else:
            bound = state.get("position_id")
            if bound is None:
                # A state written before position binding existed adopts the
                # current position id once; afterwards the binding is checked.
                if position_id is not None:
                    state["position_id"] = position_id
                    state["_needs_persist"] = True
            elif position_id is None or int(bound) != int(position_id):
                raise _FlipPositionMismatch(
                    f"supertrend_flip plan for {symbol} is bound to broker "
                    f"position {bound} but the broker now reports "
                    f"{position_id!r}; refusing to manage a different "
                    "position",
                    entry=entry,
                    state=state,
                )
        return deployment, plan, state, position

    @staticmethod
    def _require_flip_market_data(
        adapter: BaseLiveAdapter,
        timeframe: str,
    ) -> None:
        get_market_data = getattr(adapter, "get_market_data", None)
        serves = getattr(adapter, "supports_market_data_timeframe", None)
        if not callable(get_market_data) or (
            callable(serves) and not serves(timeframe)
        ):
            raise _FlipFailure(
                f"{type(adapter).__name__} cannot serve {timeframe!r} market "
                "data, so the flip workflow can never run for this deployment",
                permanent=True,
            )

    # ── workflow bookkeeping ─────────────────────────────────────────────

    def _workflow_for(self, key: tuple[str, str], signal_id: str) -> _Workflow:
        workflow = self._workflows.get(key)
        if workflow is None or workflow.signal_id != signal_id:
            workflow = _Workflow(signal_id=signal_id)
            self._workflows[key] = workflow
        return workflow

    @staticmethod
    def _record_success(workflow: _Workflow) -> None:
        workflow.outage.failures = 0
        workflow.outage.first_failure_at = None
        workflow.outage.given_up = False
        workflow.outage.permanent = False
        workflow.outage.next_probe_at = None
        workflow.retry_at = None
        workflow.retry_delay = FLIP_RETRY_INITIAL_SECONDS

    @staticmethod
    def _breakeven_due(
        plan: SupertrendFlipPlan,
        state: Mapping[str, Any],
        bar_seconds: int,
        moment: datetime,
    ) -> bool:
        """Whether the stop may be amended to breakeven this pass.

        The TP1 bar must have fully closed first - the same dead-bar rule the
        replay applies to the trail.
        """
        if state.get("phase") not in _POST_TP1_PHASES:
            return False
        if state.get("breakeven_done") or not plan.breakeven_after_tp1:
            return False
        tp1_bar_open = _parse_iso(state.get("tp1_bar_open"))
        if tp1_bar_open is None or bar_seconds <= 0:
            return False
        return moment >= tp1_bar_open + timedelta(seconds=bar_seconds)

    def _record_stops_failure(
        self,
        account_id: str,
        exc: BaseException,
        context: Mapping[str, Any],
        moment: datetime,
    ) -> None:
        schedule = self._schedules[account_id]
        outage = schedule.stops_outage
        if outage.given_up:
            outage.next_probe_at = moment + timedelta(seconds=FLIP_GIVEN_UP_PROBE_SECONDS)
            logger.warning(
                "Live supertrend-flip stop-listing probe still failing; "
                "staying given up",
                extra={"data": {**context, "error": repr(exc)}},
            )
            return
        if outage.first_failure_at is None:
            outage.first_failure_at = moment
        outage.failures += 1
        elapsed = (moment - outage.first_failure_at).total_seconds()
        if elapsed >= FLIP_GIVE_UP_AFTER_SECONDS:
            outage.given_up = True
            outage.next_probe_at = moment + timedelta(seconds=FLIP_GIVEN_UP_PROBE_SECONDS)
            self._surface(
                "Live supertrend-flip protective-stop listing given up on; "
                "the broker-side stop stays at its last level",
                exc,
                {**context, "scope": "account protective-stop listing"},
                critical=True,
            )
            return
        self._surface(
            "Live supertrend-flip protective-stop listing failed; retried",
            exc,
            {**context, "scope": "account protective-stop listing"},
        )

    async def _record_failure(
        self,
        key: tuple[str, str],
        entry: Mapping[str, Any] | None,
        exc: BaseException,
        context: dict[str, Any],
        moment: datetime,
    ) -> None:
        """Surface one failed pass and back the position off."""
        workflow = self._workflows.get(key)
        if entry is not None and (
            workflow is None or workflow.signal_id != str(entry["signal_id"])
        ):
            workflow = _Workflow(signal_id=str(entry["signal_id"]))
            self._workflows[key] = workflow
        permanent = isinstance(exc, _FlipFailure) and exc.permanent
        if permanent:
            if workflow is not None:
                workflow.outage.given_up = True
                workflow.outage.permanent = True
                workflow.outage.next_probe_at = None
            self._surface(
                "Live supertrend-flip halted for this position; the "
                "broker-side stop stays in force and the workflow will not "
                "act on guesses",
                exc,
                context,
                critical=True,
            )
            if entry is not None:
                await self._persist_halt(entry, exc)
            return
        if workflow is None:
            self._surface(
                "Live supertrend-flip pass failed; retried", exc, context
            )
            return
        outage = workflow.outage
        if outage.given_up:
            outage.next_probe_at = moment + timedelta(seconds=FLIP_GIVEN_UP_PROBE_SECONDS)
            logger.warning(
                "Live supertrend-flip probe still failing; staying given up",
                extra={"data": {**context, "error": repr(exc)}},
            )
            return
        if outage.first_failure_at is None:
            outage.first_failure_at = moment
        outage.failures += 1
        context["consecutive_failures"] = outage.failures
        elapsed = (moment - outage.first_failure_at).total_seconds()
        if elapsed >= FLIP_GIVE_UP_AFTER_SECONDS:
            outage.given_up = True
            outage.next_probe_at = moment + timedelta(seconds=FLIP_GIVEN_UP_PROBE_SECONDS)
            self._surface(
                "Live supertrend-flip given up on for this position; the "
                "broker-side stop stays at its last level and the workflow is "
                "no longer retried until a probe succeeds or the position or "
                "its entry signal changes",
                exc,
                context,
                critical=True,
            )
            return
        self._surface(
            "Live supertrend-flip step failed; the broker-side stop stays in "
            "force and the step is retried",
            exc,
            context,
        )
        workflow.retry_at = moment + timedelta(seconds=workflow.retry_delay)
        workflow.retry_delay = min(workflow.retry_delay * 2.0, FLIP_RETRY_MAX_SECONDS)

    async def _persist_halt(self, entry: Mapping[str, Any], exc: BaseException) -> None:
        """Best-effort: mark the workflow halted so a restart does not retry."""
        signal_id = str(entry.get("signal_id") or "")
        if not signal_id:
            return
        raw_state = (entry.get("metadata") or {}).get(self._state_key)
        halted = dict(raw_state) if isinstance(raw_state, dict) else _fresh_state(0.0, 0.0)
        halted["phase"] = PHASE_HALTED
        halted["halt_reason"] = repr(exc)[:500]
        try:
            await asyncio.to_thread(
                merge_live_signal_metadata,
                signal_id,
                {self._state_key: halted},
            )
        except Exception:  # except-kind: money-decision
            logger.warning(
                "Could not persist supertrend_flip halt state; the in-memory "
                "halt still applies this run",
                extra={"data": {"signal_id": signal_id}},
            )

    async def _mark_done_by_signal_id(self, signal_id: str) -> None:
        """Best-effort ``done`` mark for a workflow whose position vanished."""
        try:
            metadata = await asyncio.to_thread(get_live_signal_metadata, signal_id)
        except Exception:  # except-kind: money-decision
            metadata = None
        state = (metadata or {}).get(self._state_key)
        if not isinstance(state, dict):
            return
        if state.get("phase") in {PHASE_DONE, PHASE_HALTED}:
            return
        done = dict(state)
        done["phase"] = PHASE_DONE
        try:
            await asyncio.to_thread(
                merge_live_signal_metadata,
                signal_id,
                {self._state_key: done},
            )
        except Exception:  # except-kind: money-decision
            logger.warning(
                "Could not persist supertrend_flip done state",
                extra={"data": {"signal_id": signal_id}},
            )

    async def _mark_position_done(
        self, entry: Mapping[str, Any], state: Mapping[str, Any]
    ) -> None:
        """Persist ``done`` for a position the workflow must never touch again."""
        signal_id = str(entry.get("signal_id") or "")
        if not signal_id:
            return
        done = dict(state)
        done["phase"] = PHASE_DONE
        try:
            await asyncio.to_thread(
                merge_live_signal_metadata,
                signal_id,
                {self._state_key: done},
            )
        except Exception:  # except-kind: money-decision
            logger.warning(
                "Could not persist supertrend_flip done state",
                extra={"data": {"signal_id": signal_id}},
            )

    # ── state persistence ────────────────────────────────────────────────

    async def _persist_state(
        self, signal_id: str, state: Mapping[str, Any]
    ) -> None:
        clean = {key: value for key, value in state.items() if not key.startswith("_")}
        persisted = await asyncio.to_thread(
            merge_live_signal_metadata, signal_id, {self._state_key: clean}
        )
        if not persisted:
            raise _FlipFailure(
                f"could not persist supertrend_flip state on signal {signal_id}"
            )

    async def _position_proven_for_entry(
        self,
        adapter: BaseLiveAdapter,
        position: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> bool:
        """True only when broker evidence ties ``position`` to ``entry``.

        First sight must never bind by size: the position's opening order
        has to resolve to the persisted entry signal id (cTrader proves it
        through ``clientOrderId``/order-id attribution, the same mechanism
        the exit path uses).

        Returns ``False`` ONLY on proven foreign ownership - the broker read
        the position's orders and none resolves to this entry's signal. An
        adapter that cannot prove, a position/entry missing the ids needed
        for the lookup, and any read that fails transiently raise instead:
        the caller's retry schedule then keeps the protective stop in place
        and the plan recoverable, rather than permanently marking ``done``
        on a broker hiccup. Only a proven-foreign position ends the plan.
        """
        position_id = self._position_id(position)
        signal_id = str(entry.get("signal_id") or "").strip()
        if position_id is None or not signal_id:
            raise _FlipFailure(
                "flip first-sight provenance cannot run: the position or "
                "entry is missing the ids the broker lookup needs",
            )
        provenance = getattr(adapter, "opening_signal_id_for_position", None)
        if not callable(provenance):
            raise _FlipFailure(
                f"{type(adapter).__name__} cannot prove position "
                "provenance; flip first sight cannot bind safely",
            )
        try:
            opened_by = await provenance(int(position_id))
        except NotImplementedError as exc:
            # The base default raises: an adapter that never implemented the
            # read cannot prove anything, so first sight must retry like any
            # other transient failure - never bind, never mark ``done``.
            raise _FlipFailure(
                f"{type(adapter).__name__} cannot prove position "
                "provenance; flip first sight cannot bind safely",
            ) from exc
        return str(opened_by or "").strip() == signal_id

    # ── one position ─────────────────────────────────────────────────────

    async def _manage_position(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        entry: Mapping[str, Any],
        position: Mapping[str, Any],
        plan: SupertrendFlipPlan,
        state: dict[str, Any],
        stops: list[ProtectiveStop] | None,
        bar_seconds: int,
        moment: datetime,
        context: dict[str, Any],
    ) -> None:
        """Advance one position's workflow; raises ``_FlipFailure`` on error."""
        symbol = _normalize_symbol(position.get("symbol"))
        context["timeframe"] = deployment.signal_timeframe
        signal_id = str(entry["signal_id"])
        qty = abs(self._position_qty(position))

        if state["phase"] in {PHASE_HALTED, PHASE_DONE}:
            return
        dirty = False

        # First managed pass of a freshly adopted position: verify the broker
        # quantity against the entry signal's ledgered fills BEFORE trusting
        # the plan, then persist the binding. A mismatch means the position
        # is not what the entry opened (manual add, stale read, netting) -
        # halt rather than manage a guess.
        needs_persist = state.pop("_needs_persist", False)
        verify_entry = state.pop("_verify_entry_qty", False)
        if needs_persist or verify_entry:
            if verify_entry:
                filled = float(
                    await asyncio.to_thread(
                        get_live_signal_filled_qty, signal_id
                    )
                    or 0.0
                )
                tolerance = self._qty_tolerance(adapter, symbol)
                # Only a POSITIVE ledger can disprove the adoption: an empty
                # ledger (fills not yet reconciled, or a test fixture) is
                # "cannot verify", not "mismatch" - the position-id binding
                # still applies from here on.
                if filled > 0.0 and abs(qty - filled) > tolerance:
                    raise _FlipFailure(
                        f"broker position qty {qty} does not match the entry "
                        f"signal's ledgered fills {filled}; refusing to "
                        "adopt the position",
                        permanent=True,
                    )
            await self._persist_state(signal_id, state)

        # ── TP1 ──────────────────────────────────────────────────────────
        if state["phase"] in _TP1_PHASES:
            if await self._tp1_phase(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=self._position_id(position),
                qty=qty,
                plan=plan,
                state=state,
                bar_seconds=bar_seconds,
                moment=moment,
                context=context,
            ):
                return
            dirty = True

        # ── post-TP1: breakeven, then Supertrend bars ────────────────────
        if state["phase"] in _POST_TP1_PHASES:
            dirty, finished = await self._post_tp1_phase(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position=position,
                plan=plan,
                state=state,
                stops=stops,
                bar_seconds=bar_seconds,
                moment=moment,
                context=context,
                dirty=dirty,
                qty=qty,
            )
            if finished:
                return

        if dirty:
            await self._persist_state(signal_id, state)

    async def _tp1_phase(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        qty: float,
        plan: SupertrendFlipPlan,
        state: dict[str, Any],
        bar_seconds: int,
        moment: datetime,
        context: dict[str, Any],
    ) -> bool:
        """Run the TP1 step; ``True`` when the pass ends here.

        A still-pending or still-untriggered TP1 persists any progress and
        ends the pass - the broker-side stop protects the whole position.
        """
        dirty = await self._step_tp1(
            adapter=adapter,
            deployment=deployment,
            symbol=symbol,
            signal_id=signal_id,
            position_id=position_id,
            qty=qty,
            plan=plan,
            state=state,
            bar_seconds=bar_seconds,
            moment=moment,
            context=context,
        )
        if state["phase"] in {PHASE_TP1_PENDING, PHASE_BEFORE_TP1}:
            if dirty:
                await self._persist_state(signal_id, state)
            return True
        return False

    async def _post_tp1_phase(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position: Mapping[str, Any],
        plan: SupertrendFlipPlan,
        state: dict[str, Any],
        stops: list[ProtectiveStop] | None,
        bar_seconds: int,
        moment: datetime,
        context: dict[str, Any],
        dirty: bool,
        qty: float,
    ) -> tuple[bool, bool]:
        """Breakeven, bar evaluation and the remainder close.

        Returns ``(dirty, finished)``: ``finished`` means the pass already
        persisted and must not run the trailing persist.
        """
        if (
            self._breakeven_due(plan, state, bar_seconds, moment)
            and not state.get("breakeven_done")
            and stops is not None
        ):
            amended = await self._maybe_breakeven(
                adapter=adapter,
                symbol=symbol,
                position=position,
                state=state,
                stops=stops,
                context=context,
            )
            if amended is None:
                # Shutdown refused the mutation: persist progress and
                # retry next pass.
                if dirty:
                    await self._persist_state(signal_id, state)
                return dirty, True
            state["breakeven_done"] = True
            dirty = True
            logger.info(
                "Moved live flip stop to breakeven",
                extra={
                    "data": {
                        **context,
                        "entry_price": self._entry_price(position, state),
                    }
                },
            )

        if state["phase"] != PHASE_EXIT_PENDING:
            flipped = await self._step_bars(
                adapter=adapter,
                symbol=symbol,
                timeframe=deployment.signal_timeframe,
                plan=plan,
                state=state,
                bar_seconds=bar_seconds,
                moment=moment,
                context=context,
            )
            dirty = dirty or flipped

        if state["phase"] == PHASE_EXIT_PENDING:
            # Persist the decision BEFORE submitting: a crash after the
            # submit but before this write still recovers through the
            # deterministic signal id.
            if dirty:
                await self._persist_state(signal_id, state)
                dirty = False
            await self._step_remainder_close(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=self._position_id(position),
                qty=qty,
                state=state,
                moment=moment,
                context=context,
            )
            dirty = True
        return dirty, False

    # ── TP1 ──────────────────────────────────────────────────────────────

    async def _step_tp1(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        qty: float,
        plan: SupertrendFlipPlan,
        state: dict[str, Any],
        bar_seconds: int,
        moment: datetime,
        context: dict[str, Any],
    ) -> bool:
        """TP1 trigger, pending resolution and confirmation. Returns dirty."""
        dirty = False
        if not state.get("entry_qty"):
            state["entry_qty"] = qty
            dirty = True

        if state["phase"] == PHASE_TP1_PENDING:
            resolved = await self._resolve_tp1_pending(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=position_id,
                state=state,
                qty=qty,
                bar_seconds=bar_seconds,
                moment=moment,
                context=context,
            )
        else:
            resolved = await self._trigger_tp1(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=position_id,
                qty=qty,
                plan=plan,
                state=state,
                moment=moment,
                context=context,
            )
        return dirty or resolved

    async def _resolve_tp1_pending(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        state: dict[str, Any],
        qty: float,
        bar_seconds: int,
        moment: datetime,
        context: dict[str, Any],
    ) -> bool:
        """Resolve a submitted TP1 close; ``True`` when state changed."""
        outcome = await self._confirm_pending_close(
            adapter=adapter,
            symbol=symbol,
            state=state,
            pending_key="tp1",
            kind="tp1",
            event="",
            entry_signal_id=signal_id,
            entry_qty=float(state.get("entry_qty") or qty),
            qty=qty,
            moment=moment,
            context=context,
        )
        if outcome in {"confirmed", "confirmed_partial"}:
            state["phase"] = PHASE_AWAIT_GREEN
            trigger = _parse_iso(state.get("tp1_trigger_time")) or moment
            state["tp1_time"] = _iso(trigger)
            tp1_bar_open = await self._tp1_bar_open(
                adapter=adapter,
                symbol=symbol,
                timeframe=deployment.signal_timeframe,
                trigger=trigger,
                bar_seconds=bar_seconds,
                context=context,
            )
            if tp1_bar_open is not None:
                state["tp1_bar_open"] = _iso(tp1_bar_open)
                # The TP1 bar is dead for the workflow: bar evaluation
                # starts on the bar after it (replay parity).
                state["last_bar_time"] = state["tp1_bar_open"]
            state["tp1_order_id"] = None
            state["tp1_signal_id"] = None
            state["tp1_pending_since"] = None
            return True
        if outcome == "retry_terminal":
            await self._resubmit_close(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=position_id,
                qty=float(state.get("tp1_qty") or 0.0),
                kind="tp1",
                event="",
                state=state,
                pending_key="tp1",
                moment=moment,
                context=context,
            )
            return True
        if outcome == "resubmit_same":
            # Pending state lost its order id (a crash between submit and
            # persist): resubmit under the SAME deterministic id - the
            # adapter's persisted-order recovery answers with the existing
            # order id if one exists, so this cannot double-close.
            order_id = await self._submit_close(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=position_id,
                qty=float(state.get("tp1_qty") or 0.0),
                kind="tp1",
                event="",
                attempt=int(state.get("tp1_attempts") or 0),
                reference_price=None,
                context=context,
            )
            state["tp1_order_id"] = order_id
            state["tp1_signal_id"] = str(
                _flip_signal_id(
                    "tp1", signal_id, attempt=int(state.get("tp1_attempts") or 0)
                )
            )
            state["tp1_pending_since"] = _iso(moment)
            return True
        # "waiting": order still working or state unknown; nothing to do.
        return False

    async def _trigger_tp1(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        qty: float,
        plan: SupertrendFlipPlan,
        state: dict[str, Any],
        moment: datetime,
        context: dict[str, Any],
    ) -> bool:
        """PHASE_BEFORE_TP1: quote check, then maybe submit the tranche."""
        if state.get("tp1_skipped_time") is not None:
            # TP1 was already ruled unexecutable (tranche or step floor):
            # stay disarmed, replay parity, and never re-submit.
            return False
        get_quote = getattr(adapter, "get_quote", None)
        if not callable(get_quote):
            raise _FlipFailure(
                f"{type(adapter).__name__} cannot provide a quote for TP1 "
                "detection",
                permanent=True,
            )
        try:
            quote = await get_quote(symbol)
            bid, _ask = validate_live_quote_prices(
                quote, symbol=symbol, label="Flip TP1 quote"
            )
        except asyncio.CancelledError:
            raise
        except NotImplementedError as exc:
            raise _FlipFailure(str(exc), permanent=True) from exc
        except Exception as exc:  # except-kind: money-decision
            raise _FlipFailure(f"TP1 quote unavailable for {symbol}: {exc!r}") from exc
        if bid < plan.tp1_price:
            return False

        tranche = tp1_partial_quantity(qty, plan.tp1_exit_fraction)
        if tranche <= 0.0:
            if state.get("tp1_skipped_time") is None:
                state["tp1_skipped_time"] = _iso(moment)
                logger.info(
                    "supertrend_flip TP1 tranche floored to zero; TP1 skipped "
                    "and the workflow stays disarmed (replay parity)",
                    extra={"data": {**context, "qty": qty}},
                )
                return True
            return False

        close_qty = qty if tranche >= qty else tranche
        close_qty = self._floor_exit_qty(adapter, symbol, close_qty)
        if close_qty <= self._qty_tolerance(adapter, symbol):
            # The tranche exists in qty units but floors below one broker
            # volume step: TP1 can never execute - the replay's "skipped"
            # answer. The whole position keeps its original stop.
            state["tp1_skipped_time"] = _iso(moment)
            self._surface(
                "supertrend_flip TP1 tranche is below the broker volume step; "
                "TP1 skipped, the position keeps its original stop",
                _FlipFailure("TP1 tranche below broker volume step"),
                context,
            )
            return True

        # Persist the TP1 INTENT - phase, planned quantity, deterministic
        # signal id, trigger time - BEFORE submitting. A crash between the
        # submit and the post-submit write then restarts into
        # PHASE_TP1_PENDING with everything needed to reconcile from broker
        # truth, and can never double-close the tranche.
        attempt = int(state.get("tp1_attempts") or 0)
        state["phase"] = PHASE_TP1_PENDING
        state["tp1_qty"] = close_qty
        state["tp1_order_id"] = None
        state["tp1_signal_id"] = str(
            _flip_signal_id("tp1", signal_id, attempt=attempt)
        )
        state["tp1_trigger_time"] = _iso(moment)
        state["tp1_pending_since"] = _iso(moment)
        await self._persist_state(signal_id, state)

        try:
            order_id = await self._submit_close(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=position_id,
                qty=close_qty,
                kind="tp1",
                event="",
                attempt=attempt,
                reference_price=bid,
                context=context,
            )
        except ExitBelowStepError as exc:
            # The broker still refused the volume (a step the adapter did
            # not report): revert the intent to the skipped state so the
            # position is never resubmitted blindly.
            state["phase"] = PHASE_BEFORE_TP1
            state["tp1_qty"] = None
            state["tp1_order_id"] = None
            state["tp1_signal_id"] = None
            state["tp1_trigger_time"] = None
            state["tp1_pending_since"] = None
            state["tp1_skipped_time"] = _iso(moment)
            await self._persist_state(signal_id, state)
            self._surface(
                "supertrend_flip TP1 tranche is below the broker volume step; "
                "TP1 skipped, the position keeps its original stop",
                exc,
                context,
            )
            return True
        state["tp1_order_id"] = order_id
        logger.info(
            "Submitted live flip TP1 partial close",
            extra={
                "data": {
                    **context,
                    "tp1_qty": close_qty,
                    "position_qty": qty,
                    "broker_order_id": order_id,
                }
            },
        )
        return True

    async def _confirm_pending_close(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        state: dict[str, Any],
        pending_key: str,
        kind: str,
        event: str,
        entry_signal_id: str,
        entry_qty: float,
        qty: float,
        moment: datetime,
        context: dict[str, Any],
    ) -> str:
        """``"confirmed"`` | ``"confirmed_partial"`` | ``"waiting"`` |
        ``"retry_terminal"`` | ``"resubmit_same"``.

        Confirmation is broker truth: for TP1 the tranche's closed volume -
        measured BOTH from the position's drop AND from the execution ledger
        of every deterministic flip signal id used so far - reached the
        planned quantity; for the remainder exit the position reached zero.
        ``confirmed_partial`` means the order filled less than planned (a
        broker volume step or a partial fill): the tranche is DONE - the
        remainder keeps trailing - and the shortfall is recorded, never
        resubmitted.

        A terminal (rejected/cancelled/expired) order is retried under the
        next deterministic id; a ``filled`` order whose fills are not yet
        visible in the position read is waited on, bounded by
        ``FLIP_PENDING_TIMEOUT_SECONDS``; a pending state that names no
        order resubmits under the SAME id, which the adapter's
        persisted-order recovery makes idempotent.
        """
        tolerance = self._qty_tolerance(adapter, symbol)
        if pending_key == "tp1":
            expected_remaining = entry_qty - float(state.get("tp1_qty") or 0.0)
            done = qty <= expected_remaining + tolerance
        else:
            done = qty <= tolerance
        if done:
            return "confirmed"

        pending_since = _parse_iso(state.get(f"{pending_key}_pending_since"))
        if pending_since is not None and (
            moment - pending_since
        ).total_seconds() > FLIP_PENDING_TIMEOUT_SECONDS:
            raise _FlipFailure(
                f"{pending_key} close for {symbol} has been pending for over "
                f"{FLIP_PENDING_TIMEOUT_SECONDS // 60} minutes with the "
                "position still open; refusing to keep waiting silently"
            )

        observed = 0.0
        if pending_key == "tp1":
            # Broker-reported closed volume for the tranche: the position's
            # drop covers fresh reads; the execution ledger covers a stale
            # position read (fill recorded, quantity not yet reflected).
            closed_by_position = max(entry_qty - qty, 0.0)
            if closed_by_position > tolerance:
                observed = closed_by_position
            else:
                observed = await self._flip_ledger_filled_qty(
                    pending_key="tp1",
                    kind="tp1",
                    event="",
                    entry_signal_id=entry_signal_id,
                    state=state,
                )
            if observed > tolerance:
                planned = float(state.get("tp1_qty") or 0.0)
                state["tp1_filled_qty"] = observed
                if observed + tolerance < planned:
                    logger.warning(
                        "Live flip TP1 filled partially; the tranche is "
                        "done and the remainder keeps trailing - the "
                        "shortfall is never resubmitted",
                        extra={
                            "data": {
                                **context,
                                "planned_qty": planned,
                                "filled_qty": observed,
                            }
                        },
                    )
                return "confirmed_partial"

        order_id = state.get(f"{pending_key}_order_id")
        if not order_id:
            return "resubmit_same"
        order_state = await self._order_state(adapter, order_id, context)
        if order_state == ORDER_STATE_WORKING or order_state is None:
            return "waiting"
        if pending_key == "tp1":
            if order_state == ORDER_STATE_FILLED:
                # Filled, but neither the position nor the ledger shows the
                # tranche yet: wait (bounded by the pending timeout) rather
                # than risk a second close of the same tranche.
                return "waiting"
            return "retry_terminal"
        # exit pending: a terminal order closed nothing it should retry; a
        # FILLED order that the ledger shows only partially filled leaves a
        # live remainder to close - resubmit the CURRENT position quantity,
        # never the originally requested one.
        if order_state == ORDER_STATE_TERMINAL:
            return "retry_terminal"
        flip_signal_id = state.get("exit_signal_id")
        requested = _float_or_none(state.get("exit_qty"))
        filled = 0.0
        if flip_signal_id:
            try:
                filled = float(
                    await asyncio.to_thread(
                        get_live_signal_filled_qty, str(flip_signal_id)
                    )
                    or 0.0
                )
            except Exception as exc:  # except-kind: money-decision
                raise _FlipFailure(
                    f"flip exit fill ledger unreadable for {symbol}: {exc!r}"
                ) from exc
        if requested is not None and requested > 0.0 and filled >= requested - tolerance:
            # Fully filled; the position read is simply stale. Wait for it.
            return "waiting"
        if order_state == ORDER_STATE_FILLED and filled <= tolerance:
            # FILLED at the broker but the ledger has not caught up (and
            # the position read is stale): wait, bounded by
            # ``FLIP_PENDING_TIMEOUT_SECONDS``, rather than resubmit a
            # close the ledger cannot yet account for - the same rule TP1
            # already follows, so a lagging ledger cannot burn
            # ``FLIP_MAX_CLOSE_ATTEMPTS`` or double-close the remainder.
            return "waiting"
        return "retry_terminal"

    async def _flip_ledger_filled_qty(
        self,
        *,
        pending_key: str,
        kind: str,
        event: str,
        entry_signal_id: str,
        state: Mapping[str, Any],
    ) -> float:
        """Total quantity the execution ledger attributes to this pending op.

        Sums ``get_live_signal_filled_qty`` over every deterministic flip
        signal id used so far (attempts ``0..attempts``), so partial fills
        across retries are all counted. An unreadable ledger fails closed:
        the workflow never resubmits a close it cannot account for.
        """
        try:
            attempts = max(0, int(state.get(f"{pending_key}_attempts") or 0))
        except (TypeError, ValueError):
            attempts = 0
        total = 0.0
        for attempt in range(attempts + 1):
            flip_id = str(
                _flip_signal_id(kind, entry_signal_id, event=event, attempt=attempt)
            )
            try:
                total += float(
                    await asyncio.to_thread(get_live_signal_filled_qty, flip_id)
                    or 0.0
                )
            except Exception as exc:  # except-kind: money-decision
                raise _FlipFailure(
                    f"flip fill ledger unreadable for {flip_id}: {exc!r}"
                ) from exc
        return total

    async def _resubmit_close(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        qty: float,
        kind: str,
        event: str,
        state: dict[str, Any],
        pending_key: str,
        moment: datetime,
        context: dict[str, Any],
    ) -> None:
        """Retry a close whose order died, under the next deterministic id."""
        attempts = int(state.get(f"{pending_key}_attempts") or 0) + 1
        if attempts > FLIP_MAX_CLOSE_ATTEMPTS:
            raise _FlipFailure(
                f"{kind} close for {symbol} exhausted {FLIP_MAX_CLOSE_ATTEMPTS} "
                "attempts; halting the workflow rather than risk an "
                "unbounded close loop",
                permanent=True,
            )
        state[f"{pending_key}_attempts"] = attempts
        state[f"{pending_key}_order_id"] = None
        state[f"{pending_key}_signal_id"] = str(
            _flip_signal_id(kind, signal_id, event=event, attempt=attempts)
        )
        state[f"{pending_key}_pending_since"] = _iso(moment)
        if pending_key == "exit":
            state["exit_qty"] = qty
        # Persist the retry INTENT before submitting, same rule as the
        # initial TP1/exit submit.
        await self._persist_state(signal_id, state)
        order_id = await self._submit_close(
            adapter=adapter,
            deployment=deployment,
            symbol=symbol,
            signal_id=signal_id,
            position_id=position_id,
            qty=qty,
            kind=kind,
            event=event,
            attempt=attempts,
            reference_price=None,
            context=context,
        )
        state[f"{pending_key}_order_id"] = order_id
        logger.warning(
            f"Resubmitted live flip {kind} close after a terminal order",
            extra={
                "data": {**context, "attempt": attempts, "broker_order_id": order_id}
            },
        )

    async def _submit_close(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        qty: float,
        kind: str,
        event: str,
        attempt: int,
        reference_price: float | None,
        context: dict[str, Any],
    ) -> str:
        """Submit one workflow close through the daemon's exit chokepoints.

        Order of gates, mirroring a Discord/internal exit: the per-symbol
        exit lock serialises against other exits, the in-flight check
        defers while ANY exit for this deployment+symbol is working, and
        the shutdown mutation gate bounds the broker call itself.
        """
        flip_signal_id = _flip_signal_id(kind, signal_id, event=event, attempt=attempt)
        metadata: dict[str, Any] = {
            "exit_only": True,
            "risk_sizing_mode": EXIT_ONLY_RISK_SIZING_MODE,
            "supertrend_flip": True,
            "flip_role": kind,
            "flip_parent_signal_id": signal_id,
        }
        if event:
            metadata["flip_bar_time"] = event
        if position_id is not None:
            metadata["position_id"] = int(position_id)
        signal = LiveSignal(
            id=flip_signal_id,
            deployment_id=str(deployment.id),
            symbol=symbol,
            action="sell",
            theoretical_price=float(reference_price or 0.0),
            timestamp=datetime.now(timezone.utc),
            metadata=metadata,
        )

        async def _place() -> str:
            # Same serialization the daemon's own exits get: the in-flight
            # check and the submit are one section under the account's
            # order-placement lock, inside the per-symbol exit lock, so a
            # concurrent entry sizing or flatten cannot interleave between
            # the read and the close. Lock order matches the daemon:
            # ``_exit_lock`` outer, account lock inner.
            async with account_order_placement(str(deployment.account_id)):
                await self._check_flip_exit_clear(
                    adapter, deployment, symbol, context
                )
                if self._mutation_gate is not None:
                    async with self._mutation_gate() as may_mutate:
                        if not may_mutate:
                            raise _FlipShutdownRefused(
                                "daemon is shutting down; flip close not submitted"
                            )
                        return await adapter.submit_order(signal, qty=qty)
                return await adapter.submit_order(signal, qty=qty)

        if self._exit_lock is None:
            return await _place()
        account_id = str(context.get("account_id") or "")
        async with self._exit_lock(account_id, symbol):
            return await _place()

    async def _check_flip_exit_clear(
        self,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        context: dict[str, Any],
    ) -> None:
        """Defer the close while another exit for this position is working."""
        if self._exit_in_flight_check is None:
            return
        try:
            blockers = await self._exit_in_flight_check(
                adapter, deployment, symbol
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: money-decision
            raise _FlipFailure(
                f"could not verify in-flight exits for {symbol}: {exc!r}"
            ) from exc
        if blockers:
            raise _FlipDeferredClose(
                f"an exit order is already working for {symbol}; deferring "
                "the flip close"
            )

    # ── breakeven ────────────────────────────────────────────────────────

    async def _maybe_breakeven(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        position: Mapping[str, Any],
        state: dict[str, Any],
        stops: list[ProtectiveStop],
        context: dict[str, Any],
    ) -> bool | None:
        """Amend the stop to the entry price once the TP1 bar has closed.

        Returns ``True`` when the broker confirms the stop at breakeven (or it
        already was); ``None`` when the shutdown gate refused the mutation and
        the pass must be retried.
        """
        entry_price = self._entry_price(position, state)
        if entry_price <= 0:
            raise _FlipFailure(
                f"cannot move the {symbol} stop to breakeven: no readable "
                "entry price",
                permanent=True,
            )
        # Flip is long-only, so the protective stop is always the sell side.
        symbol_stops = [
            stop for stop in stops if stop.symbol == symbol and stop.side == "sell"
        ]
        if len(symbol_stops) != 1:
            raise _FlipFailure(
                f"expected exactly one amendable protective stop for {symbol}, "
                f"found {len(symbol_stops)}; not amending any of them"
            )
        current = symbol_stops[0]
        tolerance = self._price_tolerance(adapter, symbol)
        if current.stop_price > entry_price + tolerance:
            # A manually tightened stop already sits ABOVE breakeven:
            # amending to entry would LOOSEN it. Leave it and mark done.
            return True
        if abs(current.stop_price - entry_price) <= tolerance:
            return True
        target = adapter.round_stop_price(current, entry_price)
        amended = await self._amend_stop(adapter, current, target)
        if amended is None:
            return None
        context["target_stop"] = amended.stop_price
        return True

    async def _amend_stop(
        self, adapter: BaseLiveAdapter, current: ProtectiveStop, target: float
    ) -> ProtectiveStop | None:
        if self._mutation_gate is None:
            return await adapter.amend_protective_stop(current, target)
        async with self._mutation_gate() as may_mutate:
            if not may_mutate:
                return None
            return await adapter.amend_protective_stop(current, target)

    # ── Supertrend bars ──────────────────────────────────────────────────

    async def _step_bars(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        timeframe: str,
        plan: SupertrendFlipPlan,
        state: dict[str, Any],
        bar_seconds: int,
        moment: datetime,
        context: dict[str, Any],
    ) -> bool:
        """Evaluate newly closed bars through the flip state machine.

        Returns whether the persisted state changed.
        """
        if bar_seconds <= 0:
            raise _FlipFailure(
                f"deployment timeframe {timeframe!r} has no bar length",
                permanent=True,
            )
        last_bar_time = _parse_iso(state.get("last_bar_time"))
        # Cheap pre-filter, alignment-agnostic: the bar after ``last_bar_time``
        # opens ~one bar later and closes ~two bars after it. Until that close
        # passes, nothing new can be decidable - skip the broker read. Broker
        # anchoring (Monday weeks, server days) never enters this arithmetic;
        # actual bar opens come from the returned frame below.
        if (
            last_bar_time is not None
            and last_bar_time + timedelta(seconds=2 * bar_seconds) > moment
        ):
            return False
        # The newest bar that has fully closed: open + length <= now. Bars
        # are broker-anchored, so decidability is judged on the RETURNED bar
        # timestamps inside ``_flip_decidable_bars`` - never on epoch arithmetic,
        # which misaligns on broker weeks (Monday-anchored).
        get_market_data = getattr(adapter, "get_market_data", None)
        if not callable(get_market_data):
            raise _FlipFailure(
                f"{type(adapter).__name__} provides no market data",
                permanent=True,
            )
        lookback = max(
            FLIP_BAR_LOOKBACK, plan.supertrend_period * 30, plan.supertrend_period + 30
        )
        frame = await get_market_data(symbol, timeframe=timeframe, limit=lookback)
        bars = self._flip_decidable_bars(
            frame,
            plan=plan,
            bar_seconds=bar_seconds,
            moment=moment,
            after=last_bar_time,
            dead_at_or_before=_parse_iso(state.get("tp1_bar_open")),
        )
        if not bars:
            # Served but nothing new decidable: a late bar is retried next tick.
            return False

        # A bar is decidable only once open + length <= now; the gate on the
        # candidate's own timestamp (not epoch arithmetic) keeps weekly and
        # server-day anchored bars honest.
        candidate_open = bars[-1][0] + timedelta(seconds=bar_seconds)
        if candidate_open > moment:
            return False

        dirty = False
        for bar_time, ready, green in bars:
            if state["phase"] == PHASE_AWAIT_GREEN and ready and green:
                state["phase"] = PHASE_ACTIVE
                state["activation_time"] = _iso(bar_time)
                dirty = True
            elif state["phase"] == PHASE_ACTIVE and ready and not green:
                state["phase"] = PHASE_EXIT_PENDING
                state["flip_bar_time"] = _iso(bar_time)
                state["last_bar_time"] = _iso(bar_time)
                # The flip is decided at this bar's close; the remainder close
                # is submitted by the caller in the same pass (a market order,
                # the live counterpart of replay's next-bar-open fill).
                return True
            state["last_bar_time"] = _iso(bar_time)
            dirty = True
        return dirty

    @staticmethod
    def _flip_decidable_bars(
        frame: Any,
        *,
        plan: SupertrendFlipPlan,
        bar_seconds: int,
        moment: datetime,
        after: datetime | None,
        dead_at_or_before: datetime | None,
    ) -> list[tuple[datetime, bool, bool]]:
        """``(open_time, ready, green)`` for newly closed bars, oldest first.

        The Supertrend columns come from the RamJet processor's own
        implementation - the same forward recursion the replay runs - so live
        and replay can never drift onto two definitions. A bar counts once
        ``open + length <= now``; bars at or before ``after`` were already
        evaluated and bars at or before ``dead_at_or_before`` (the TP1 bar)
        are dead for the workflow by rule, not by accident.
        """
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise _FlipFailure("market data returned no bars")
        required = {"timestamp", "open", "high", "low", "close"}
        if not required.issubset(frame.columns):
            raise _FlipFailure("market data has no timestamp/OHLC columns")
        ordered = frame.sort_values("timestamp").reset_index(drop=True)
        try:
            computed = calculate_supertrend(
                ordered[["open", "high", "low", "close"]],
                plan.supertrend_period,
                plan.supertrend_multiplier,
            )
        except Exception as exc:  # except-kind: money-decision
            raise _FlipFailure(f"Supertrend computation failed: {exc!r}") from exc
        if (
            "supertrend" not in computed.columns
            or "supertrend_ready" not in computed.columns
        ):
            raise _FlipFailure(
                "Supertrend computation returned no direction/readiness columns"
            )

        bars: list[tuple[datetime, bool, bool]] = []
        for raw_time, raw_ready, raw_green in zip(
            ordered["timestamp"],
            computed["supertrend_ready"],
            computed["supertrend"],
            strict=True,
        ):
            open_time = _bar_open_utc(raw_time)
            if not _bar_decidable(
                open_time,
                moment=moment,
                bar_seconds=bar_seconds,
                after=after,
                dead_at_or_before=dead_at_or_before,
            ):
                continue
            bars.append((open_time, bool(raw_ready), bool(raw_green)))
        return bars

    # ── remainder close ──────────────────────────────────────────────────

    async def _step_remainder_close(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        symbol: str,
        signal_id: str,
        position_id: int | None,
        qty: float,
        state: dict[str, Any],
        moment: datetime,
        context: dict[str, Any],
    ) -> None:
        """Close the remainder once the flip exit has been decided."""
        tolerance = self._qty_tolerance(adapter, symbol)
        if qty <= tolerance:
            # Dust or already closed: nothing left to send.
            return
        event = str(state.get("flip_bar_time") or "")
        outcome = await self._confirm_pending_close(
            adapter=adapter,
            symbol=symbol,
            state=state,
            pending_key="exit",
            kind="flip-exit",
            event=event,
            entry_signal_id=signal_id,
            entry_qty=float(state.get("entry_qty") or qty),
            qty=qty,
            moment=moment,
            context=context,
        )
        if outcome in {"confirmed", "waiting"}:
            return
        if outcome == "retry_terminal":
            await self._resubmit_close(
                adapter=adapter,
                deployment=deployment,
                symbol=symbol,
                signal_id=signal_id,
                position_id=position_id,
                qty=qty,
                kind="flip-exit",
                event=event,
                state=state,
                pending_key="exit",
                moment=moment,
                context=context,
            )
            return
        # "resubmit_same" or no order yet: persist the exit INTENT -
        # phase was already persisted by the caller, but the deterministic
        # signal id, planned quantity and pending marker must be durable
        # BEFORE the submit so a crash cannot lose them.
        attempt = int(state.get("exit_attempts") or 0)
        state["exit_signal_id"] = str(
            _flip_signal_id("flip-exit", signal_id, event=event, attempt=attempt)
        )
        state["exit_qty"] = qty
        state["exit_pending_since"] = _iso(moment)
        state["exit_order_id"] = None
        await self._persist_state(signal_id, state)
        order_id = await self._submit_close(
            adapter=adapter,
            deployment=deployment,
            symbol=symbol,
            signal_id=signal_id,
            position_id=position_id,
            qty=qty,
            kind="flip-exit",
            event=event,
            attempt=attempt,
            reference_price=None,
            context=context,
        )
        state["exit_order_id"] = order_id
        logger.info(
            "Submitted live flip remainder close",
            extra={
                "data": {
                    **context,
                    "qty": qty,
                    "broker_order_id": order_id,
                    "flip_bar_time": event,
                }
            },
        )

    # ── small helpers ────────────────────────────────────────────────────

    @staticmethod
    def _position_id(position: Mapping[str, Any]) -> int | None:
        raw = position.get("position_id", position.get("positionId"))
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _entry_price(position: Mapping[str, Any], state: Mapping[str, Any]) -> float:
        try:
            price = float(position.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if math.isfinite(price) and price > 0:
            return price
        stored = _float_or_none(state.get("entry_price"))
        return stored if stored is not None and stored > 0 else 0.0

    @staticmethod
    def _qty_tolerance(adapter: BaseLiveAdapter, symbol: str) -> float:
        probe = getattr(adapter, "position_qty_tolerance", None)
        if callable(probe):
            try:
                value = float(probe(symbol))
                if math.isfinite(value) and value > 0:
                    return value
            except Exception:  # except-kind: money-decision
                pass
        return 1e-9

    @staticmethod
    def _price_tolerance(adapter: BaseLiveAdapter, symbol: str) -> float:
        """Half a broker price tick where the adapter reports one."""
        tick_fn = getattr(adapter, "price_tick_size", None)
        if callable(tick_fn):
            try:
                tick = float(tick_fn(symbol))
                if math.isfinite(tick) and tick > 0:
                    return tick / 2.0
            except Exception:  # except-kind: money-decision
                pass
        return 1e-9

    @staticmethod
    def _floor_exit_qty(
        adapter: BaseLiveAdapter, symbol: str, qty: float
    ) -> float:
        """Floor a close quantity to the broker's volume step, when known."""
        step_fn = getattr(adapter, "exit_qty_step", None)
        if not callable(step_fn):
            return qty
        try:
            step = float(step_fn(symbol))
        except Exception:  # except-kind: money-decision
            return qty
        if not math.isfinite(step) or step <= 0:
            return qty
        return floor_qty_to_step(qty, step)

    async def _tp1_bar_open(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        timeframe: str,
        trigger: datetime,
        bar_seconds: int,
        context: dict[str, Any],
    ) -> datetime | None:
        """The broker-aligned bar open containing ``trigger``.

        Derived from the newest returned bar whose open is at or before the
        trigger - never epoch arithmetic, so broker-anchored weeks/days stay
        honest. Falls back to the epoch floor only when market data is
        unreadable (logged); ``None`` when even that is impossible.
        """
        get_market_data = getattr(adapter, "get_market_data", None)
        if callable(get_market_data) and bar_seconds > 0:
            try:
                frame = await get_market_data(
                    symbol, timeframe=timeframe, limit=FLIP_BAR_LOOKBACK
                )
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    opens = pd.to_datetime(
                        frame["timestamp"], utc=True
                    ).sort_values()
                    idx = int(
                        opens.searchsorted(pd.Timestamp(trigger), side="right")
                    ) - 1
                    if idx >= 0:
                        return _bar_open_utc(opens.iloc[idx])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # except-kind: money-decision
                logger.warning(
                    "TP1 bar lookup failed; falling back to epoch bar open",
                    extra={"data": {**context, "error": repr(exc)}},
                )
        if bar_seconds <= 0:
            return None
        bar_open_ts = (int(trigger.timestamp()) // bar_seconds) * bar_seconds
        return datetime.fromtimestamp(bar_open_ts, tz=timezone.utc)

    async def _order_state(
        self, adapter: BaseLiveAdapter, order_id: Any, context: Mapping[str, Any]
    ) -> str | None:
        get_order_state = getattr(adapter, "get_order_state", None)
        if not callable(get_order_state):
            return None
        try:
            return await get_order_state(str(order_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: money-decision
            logger.warning(
                "Flip close order-state lookup failed; treating as unknown",
                extra={"data": {**context, "error": repr(exc)}},
            )
            return None

    # ── failure surfacing ────────────────────────────────────────────────

    @staticmethod
    def _surface(
        message: str,
        exc: BaseException,
        context: Mapping[str, Any],
        *,
        critical: bool = False,
    ) -> None:
        (logger.critical if critical else logger.error)(
            message,
            extra={"data": {**context, "error": repr(exc)}},
        )
        with contextlib.suppress(Exception):
            capture_exception(
                exc,
                service_name="live-runner",
                context={"component": "live_supertrend_flip", **context},
            )


async def run_flip_workflow_loop(
    manager: LiveSupertrendFlipManager,
    *,
    eligible_adapters: Callable[[], Mapping[str, BaseLiveAdapter]],
    current_deployments: Callable[[], Iterable[LiveDeploymentConfig]],
    interval_seconds: float = FLIP_TICK_SECONDS,
) -> None:
    """Drive ``manager`` forever. One failed tick never ends the loop."""
    while True:
        try:
            await manager.run_once(eligible_adapters(), current_deployments())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: loop-survival
            LiveSupertrendFlipManager._surface(
                "Live supertrend-flip tick failed", exc, {"phase": "tick"}
            )
        await asyncio.sleep(interval_seconds)
