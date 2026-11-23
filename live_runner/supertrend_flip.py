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

from live_runner.base_adapter import (
    EXIT_ONLY_RISK_SIZING_MODE,
    ORDER_STATE_TERMINAL,
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
    merge_live_signal_metadata,
)
from shared.logging import get_logger
from shared.models import LiveDeploymentConfig, LiveSignal
from shared.sentry import capture_exception
from shared.trailing import (
    TRAILING_MODE_SUPERTREND_FLIP,
    TRAILING_SL_CONFIG_METADATA_KEY,
    SupertrendFlipPlan,
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
_KNOWN_PHASES = frozenset(
    {
        PHASE_BEFORE_TP1,
        PHASE_TP1_PENDING,
        PHASE_AWAIT_GREEN,
        PHASE_ACTIVE,
        PHASE_EXIT_PENDING,
        PHASE_HALTED,
    }
)
_POST_TP1_PHASES = frozenset({PHASE_AWAIT_GREEN, PHASE_ACTIVE, PHASE_EXIT_PENDING})

#: How often the daemon loop should wake this manager. TP1 is an intrabar
#: trigger, so - unlike the bar-boundary trailing loop - a healthy pass runs
#: every tick; the tick only bounds quote-check latency.
FLIP_TICK_SECONDS = 15.0
#: Bars requested per Supertrend evaluation. Must cover the warm-up
#: (``supertrend_period``) plus enough history for the bands to settle; the
#: floor keeps short-period plans from starving the recursion.
FLIP_BAR_LOOKBACK = 120
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
        "exit_attempts": 0,
        "exit_pending_since": None,
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
    for key in ("entry_qty", "tp1_qty"):
        if state.get(key) is not None:
            number = _float_or_none(state.get(key))
            if number is None or number < 0:
                raise _FlipFailure(
                    f"supertrend_flip state for {context} has unreadable {key} "
                    f"({state.get(key)!r})",
                    permanent=True,
                )
            state[key] = number
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
        state_key: str = FLIP_STATE_METADATA_KEY,
    ) -> None:
        self._load_entry_signals = load_entry_signals
        self._mutation_gate = mutation_gate
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
        managed_by_account: dict[str, list[LiveDeploymentConfig]] = {}
        for deployment in deployments:
            status = str(deployment.status or "").strip().upper()
            # PAUSED still manages its open positions: every workflow action is
            # a close or a protective amendment, which a pause allows (#126).
            if status in {"ACTIVE", "PAUSED"}:
                managed_by_account.setdefault(deployment.account_id, []).append(deployment)

        eligible = {
            account_id: adapter
            for account_id, adapter in adapters.items()
            if account_id in managed_by_account
            and getattr(adapter, "supports_stop_amendment", False) is True
        }
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
            del self._workflows[key]
        if not positions:
            return

        deployments_by_id = {str(item.id): item for item in deployments}
        entry_signals = await asyncio.to_thread(
            self._load_entry_signals, sorted(deployments_by_id), sorted(positions)
        )

        stops: list[ProtectiveStop] | None = None
        stops_unavailable = False
        for symbol, rows in positions.items():
            entry = entry_signals.get(symbol)
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
                    continue
                # ``_prepare_position`` only returns a plan for a real entry.
                assert entry is not None
                deployment, plan, state, position = prepared
                workflow = self._workflow_for(key, str(entry["signal_id"]))
                if workflow.outage.probe_blocked(moment) or (
                    workflow.retry_at is not None and moment < workflow.retry_at
                ):
                    continue
                bar_seconds = _timeframe_seconds(deployment.signal_timeframe) or 0
                breakeven_due = self._breakeven_due(plan, state, bar_seconds, moment)
                if breakeven_due and stops is None and not stops_unavailable:
                    # One account-wide read shared by every due position: a
                    # failure is tracked on the account schedule, not per
                    # position.
                    try:
                        stops = list(await adapter.list_protective_stops())
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # except-kind: money-decision
                        stops_unavailable = True
                        self._record_stops_failure(
                            account_id, exc, context, moment
                        )
                if breakeven_due and stops is None:
                    continue
                await self._manage_position(
                    adapter=adapter,
                    deployment=deployment,
                    entry=entry,
                    position=position,
                    plan=plan,
                    state=state,
                    stops=stops if breakeven_due else None,
                    bar_seconds=bar_seconds,
                    moment=moment,
                    context=context,
                )
                self._record_success(workflow)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # except-kind: money-decision
                await self._record_failure(key, entry, exc, context, moment)

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
        if state is None:
            state = _fresh_state(qty, entry_price)
        return deployment, plan, state, position

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
        dirty = False

        phase = state["phase"]
        if phase == PHASE_HALTED:
            return

        # ── TP1 ──────────────────────────────────────────────────────────
        if phase in {PHASE_BEFORE_TP1, PHASE_TP1_PENDING}:
            outcome = await self._step_tp1(
                adapter=adapter,
                symbol=symbol,
                signal_id=signal_id,
                deployment_id=str(deployment.id),
                position_id=self._position_id(position),
                qty=qty,
                plan=plan,
                state=state,
                bar_seconds=bar_seconds,
                moment=moment,
                context=context,
            )
            dirty = dirty or outcome
            phase = state["phase"]
            if phase == PHASE_TP1_PENDING:
                if dirty:
                    await self._persist_state(signal_id, state)
                return
            if phase == PHASE_BEFORE_TP1:
                # TP1 skipped (zero tranche) or not yet triggered: nothing else
                # to do - the broker-side stop protects the whole position.
                if dirty:
                    await self._persist_state(signal_id, state)
                return
            dirty = True

        # ── post-TP1: breakeven, then Supertrend bars ────────────────────
        if phase in _POST_TP1_PHASES:
            if self._breakeven_due(plan, state, bar_seconds, moment) and not state.get(
                "breakeven_done"
            ):
                if stops is None:
                    # Stop listing unavailable this pass; the bars below can
                    # still be evaluated, the amendment waits.
                    pass
                else:
                    amended = await self._maybe_breakeven(
                        adapter=adapter,
                        symbol=symbol,
                        position=position,
                        state=state,
                        stops=stops,
                        moment=moment,
                        context=context,
                    )
                    if amended is None:
                        # Shutdown refused the mutation: persist progress and
                        # retry next pass.
                        if dirty:
                            await self._persist_state(signal_id, state)
                        return
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

            if phase != PHASE_EXIT_PENDING:
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
                phase = state["phase"]

            if phase == PHASE_EXIT_PENDING:
                # Persist the decision BEFORE submitting: a crash after the
                # submit but before this write still recovers through the
                # deterministic signal id.
                if dirty:
                    await self._persist_state(signal_id, state)
                    dirty = False
                await self._step_remainder_close(
                    adapter=adapter,
                    symbol=symbol,
                    signal_id=signal_id,
                    deployment_id=str(deployment.id),
                    position_id=self._position_id(position),
                    qty=qty,
                    state=state,
                    moment=moment,
                    context=context,
                )
                dirty = True

        if dirty:
            await self._persist_state(signal_id, state)

    # ── TP1 ──────────────────────────────────────────────────────────────

    async def _step_tp1(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        signal_id: str,
        deployment_id: str,
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
            outcome = await self._confirm_pending_close(
                adapter=adapter,
                symbol=symbol,
                state=state,
                pending_key="tp1",
                expected_remaining=(
                    float(state.get("entry_qty") or qty)
                    - float(state.get("tp1_qty") or 0.0)
                ),
                qty=qty,
                moment=moment,
                context=context,
            )
            if outcome == "confirmed":
                state["phase"] = PHASE_AWAIT_GREEN
                trigger = _parse_iso(state.get("tp1_trigger_time")) or moment
                state["tp1_time"] = _iso(trigger)
                if bar_seconds > 0:
                    bar_open_ts = (int(trigger.timestamp()) // bar_seconds) * bar_seconds
                    state["tp1_bar_open"] = _iso(
                        datetime.fromtimestamp(bar_open_ts, tz=timezone.utc)
                    )
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
                    symbol=symbol,
                    signal_id=signal_id,
                    deployment_id=deployment_id,
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
                    symbol=symbol,
                    signal_id=signal_id,
                    deployment_id=deployment_id,
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
            return dirty

        # PHASE_BEFORE_TP1: quote check, then maybe submit the tranche.
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
            return dirty

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
            return dirty

        close_qty = qty if tranche >= qty else tranche
        try:
            order_id = await self._submit_close(
                adapter=adapter,
                symbol=symbol,
                signal_id=signal_id,
                deployment_id=deployment_id,
                position_id=position_id,
                qty=close_qty,
                kind="tp1",
                event="",
                attempt=int(state.get("tp1_attempts") or 0),
                reference_price=bid,
                context=context,
            )
        except ExitBelowStepError as exc:
            # The tranche exists in qty units but is below one broker volume
            # step: TP1 can never execute, which is the replay's "skipped"
            # answer. The whole position keeps its original stop.
            if state.get("tp1_skipped_time") is None:
                state["tp1_skipped_time"] = _iso(moment)
            self._surface(
                "supertrend_flip TP1 tranche is below the broker volume step; "
                "TP1 skipped, the position keeps its original stop",
                exc,
                context,
            )
            return True
        state["phase"] = PHASE_TP1_PENDING
        state["tp1_qty"] = close_qty
        state["tp1_order_id"] = order_id
        state["tp1_signal_id"] = str(
            _flip_signal_id("tp1", signal_id, attempt=int(state.get("tp1_attempts") or 0))
        )
        state["tp1_trigger_time"] = _iso(moment)
        state["tp1_pending_since"] = _iso(moment)
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
        expected_remaining: float,
        qty: float,
        moment: datetime,
        context: dict[str, Any],
    ) -> str:
        """``"confirmed"`` | ``"waiting"`` | ``"retry_terminal"`` | ``"resubmit_same"``.

        Confirmation is broker truth: the open quantity dropped to the
        expected remainder. A terminal order is retried under the next
        deterministic id (a fill would already have shown in the quantity, so
        terminal here means the order died); a pending state that names no
        order resubmits under the SAME id, which the adapter's
        persisted-order recovery makes idempotent; a working or unknown one is
        waited on, bounded by ``FLIP_PENDING_TIMEOUT_SECONDS``.
        """
        tolerance = self._qty_tolerance(adapter, symbol)
        done = qty <= expected_remaining + tolerance
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

        order_id = state.get(f"{pending_key}_order_id")
        if not order_id:
            return "resubmit_same"
        order_state = await self._order_state(adapter, order_id, context)
        if order_state == ORDER_STATE_TERMINAL:
            return "retry_terminal"
        return "waiting"

    async def _resubmit_close(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        signal_id: str,
        deployment_id: str,
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
        order_id = await self._submit_close(
            adapter=adapter,
            symbol=symbol,
            signal_id=signal_id,
            deployment_id=deployment_id,
            position_id=position_id,
            qty=qty,
            kind=kind,
            event=event,
            attempt=attempts,
            reference_price=None,
            context=context,
        )
        state[f"{pending_key}_order_id"] = order_id
        state[f"{pending_key}_signal_id"] = str(
            _flip_signal_id(kind, signal_id, event=event, attempt=attempts)
        )
        state[f"{pending_key}_pending_since"] = _iso(moment)
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
        symbol: str,
        signal_id: str,
        deployment_id: str,
        position_id: int | None,
        qty: float,
        kind: str,
        event: str,
        attempt: int,
        reference_price: float | None,
        context: dict[str, Any],
    ) -> str:
        """Submit one workflow close under the shutdown mutation gate."""
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
            deployment_id=deployment_id,
            symbol=symbol,
            action="sell",
            theoretical_price=float(reference_price or 0.0),
            timestamp=datetime.now(timezone.utc),
            metadata=metadata,
        )
        if self._mutation_gate is None:
            return await adapter.submit_order(signal, qty=qty)
        async with self._mutation_gate() as may_mutate:
            if not may_mutate:
                raise _FlipFailure("daemon is shutting down; flip close not submitted")
            return await adapter.submit_order(signal, qty=qty)

    # ── breakeven ────────────────────────────────────────────────────────

    async def _maybe_breakeven(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        position: Mapping[str, Any],
        state: dict[str, Any],
        stops: list[ProtectiveStop],
        moment: datetime,
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
        if abs(current.stop_price - entry_price) <= self._price_tolerance(adapter, symbol):
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
        # The newest bar that has fully closed: open + length <= now.
        candidate_ts = (moment.timestamp() - bar_seconds) // bar_seconds * bar_seconds
        candidate_open = datetime.fromtimestamp(candidate_ts, tz=timezone.utc)
        if last_bar_time is not None and last_bar_time >= candidate_open:
            return False

        get_market_data = getattr(adapter, "get_market_data", None)
        if not callable(get_market_data):
            raise _FlipFailure(
                f"{type(adapter).__name__} provides no market data",
                permanent=True,
            )
        lookback = max(FLIP_BAR_LOOKBACK, plan.supertrend_period + 30)
        frame = await get_market_data(symbol, timeframe=timeframe, limit=lookback)
        bars = self._supertrend_bars(
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
    def _supertrend_bars(
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
            opened = pd.Timestamp(raw_time)
            if opened.tzinfo is None:
                opened = opened.tz_localize("UTC")
            else:
                opened = opened.tz_convert("UTC")
            open_time = opened.to_pydatetime()
            if open_time + timedelta(seconds=bar_seconds) > moment:
                continue
            if after is not None and open_time <= after:
                continue
            if dead_at_or_before is not None and open_time <= dead_at_or_before:
                continue
            bars.append((open_time, bool(raw_ready), bool(raw_green)))
        return bars

    # ── remainder close ──────────────────────────────────────────────────

    async def _step_remainder_close(
        self,
        *,
        adapter: BaseLiveAdapter,
        symbol: str,
        signal_id: str,
        deployment_id: str,
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
            expected_remaining=0.0,
            qty=qty,
            moment=moment,
            context=context,
        )
        if outcome == "confirmed":
            return
        if outcome == "waiting":
            return
        if outcome == "retry_terminal":
            await self._resubmit_close(
                adapter=adapter,
                symbol=symbol,
                signal_id=signal_id,
                deployment_id=deployment_id,
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
        # "resubmit_same" or no order yet: submit under the current attempt id
        # (idempotent - the adapter returns the persisted order id if the same
        # signal was already submitted).
        order_id = await self._submit_close(
            adapter=adapter,
            symbol=symbol,
            signal_id=signal_id,
            deployment_id=deployment_id,
            position_id=position_id,
            qty=qty,
            kind="flip-exit",
            event=event,
            attempt=int(state.get("exit_attempts") or 0),
            reference_price=None,
            context=context,
        )
        state["exit_order_id"] = order_id
        state["exit_signal_id"] = str(
            _flip_signal_id(
                "flip-exit",
                signal_id,
                event=event,
                attempt=int(state.get("exit_attempts") or 0),
            )
        )
        state["exit_pending_since"] = _iso(moment)
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
        return 1e-9

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


async def run_supertrend_flip_loop(
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
