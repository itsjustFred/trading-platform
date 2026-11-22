"""The operator kill switch (#300): a persisted HALT the daemon honours.

One halt row per scope in ``live_kill_switch`` (see ``shared/database.py``): the
reserved ``global`` scope plus one scope per live account. Two checkpoints read
it, and both are fail closed:

* **Before every entry.** :func:`refuse_entry_while_halted` is called from
  ``prepare_order_with_risk``, the single chokepoint every entry order of every
  deployment - Discord and internal alike - passes through. It runs *after* the
  exit branch there, so closing a position is never blocked by a halt: trapping
  an operator in an open position is the opposite of what the feature is for.
* **On every tick.** :func:`enforce_live_kill_switch` is called from the
  deployment-sync loop, so a halt written while nothing was running is enforced
  by the first tick after a restart.

Fail closed means exactly one thing, and it is deliberately the cheap one: a
halt state that cannot be read, or a stored mode that cannot be interpreted,
refuses **entries**. It never sweeps the broker. Liquidating an account on a
database error would be a real money event the operator never asked for, and an
empty read is indistinguishable from "nothing is halted" - the one answer a kill
switch must never produce by accident.

The state is read from the database on every use and never cached in the
process, because the point of a halt is the window it covers: a deployment, a
restart, or the daemon being gone altogether. The one thing kept in memory is
the sweep ledger (which account was already swept in which mode), and that lives
on the :class:`KillSwitchState` the daemon refreshes in place once per tick, so
a flat account is not re-flattened every tick while an escalation still takes
effect at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Any

from live_runner.base_adapter import BaseLiveAdapter
from shared.database import (
    KILL_SWITCH_GLOBAL_SCOPE,
    read_live_kill_switch_scopes,
)
from shared.logging import get_logger

logger = get_logger("live-runner")

#: How often a halted account is re-swept while the halt stands. Broker I/O, and
#: a sweep that completed leaves the account flat with entries refused, so the
#: interval bounds how long a broker-side order placed by hand can survive. The
#: deployment-sync loop runs every 10 s, so this is roughly every third tick.
KILL_SWITCH_SWEEP_INTERVAL_SECONDS = 30.0

#: A broker-side ``pending_cancel`` order normally settles within seconds. A
#: halted account stays unmet while one is listed (it could still fill); after
#: this many consecutive sweep passes the WARNING becomes an ERROR so a stuck
#: cancel reaches an operator - the same rule STOPPED enforcement applies to
#: ``_PENDING_CANCEL_PASSES`` in the daemon, which imports this constant.
PENDING_CANCEL_ESCALATION_PASSES = 6


class KillSwitchMode(Enum):
    """What a halt forbids, least restrictive first.

    The declaration order is the strictness ranking
    :func:`strictest_kill_switch_mode` uses to combine a global halt with a
    per-account one, and it matches ``shared.database.KILL_SWITCH_MODES`` (the
    layer that stores a mode and has to refuse one it cannot interpret). The test
    ``test_the_stored_modes_and_the_runner_modes_are_the_same_ranking`` pins the
    two together so neither can drift.
    """

    #: Refuse new entries. Working orders and open positions are left alone, so
    #: an operator can stop adding risk without liquidating.
    ENTRIES = "entries"
    #: Also cancel every working order.
    ORDERS = "orders"
    #: Also close every open position and sweep the protections the flatten
    #: orphaned - the same control-plane sequence STOPPED enforcement runs.
    FLATTEN = "flatten"

    @classmethod
    def parse(cls, value: object) -> KillSwitchMode | None:
        """``value`` as a mode, or ``None`` when it names none.

        Padding and case are an operator typing, not a different mode. A value
        that is not a mode is reported as unknown rather than guessed at, and
        the callers that cannot proceed on an unknown mode fail closed.
        """
        if isinstance(value, cls):
            return value
        if value is None:
            return None
        text = str(value).strip().lower()
        for mode in cls:
            if mode.value == text:
                return mode
        return None

    @property
    def strictness(self) -> int:
        """Rank in the least-restrictive-first order declared above."""
        return list(KillSwitchMode).index(self)

    @property
    def cancels_orders(self) -> bool:
        """Whether this mode has to touch the broker to cancel working orders."""
        return self is not KillSwitchMode.ENTRIES

    @property
    def flattens(self) -> bool:
        """Whether this mode has to touch the broker to close positions."""
        return self is KillSwitchMode.FLATTEN


class LiveHaltError(RuntimeError):
    """An entry was refused because the account is halted.

    Raised before any broker state is read, so a halted account costs no
    round-trip. Carries the halt's own fields so a caller can report the halt
    rather than a generic refusal.
    """

    def __init__(
        self,
        message: str,
        *,
        account_id: str,
        mode: KillSwitchMode,
        state_unreadable: bool = False,
    ) -> None:
        super().__init__(message)
        self.account_id = account_id
        self.mode = mode
        self.state_unreadable = state_unreadable


def strictest_kill_switch_mode(
    modes: Iterable[KillSwitchMode | str | None],
) -> KillSwitchMode | None:
    """The strictest of ``modes``, or ``None`` when there is none.

    How a global halt and a per-account one combine: the strictest wins, so a
    per-account write may only ever add restriction. The other reading - an
    account scope relaxing a platform-wide halt - would let one account's write
    quietly resume trading on an account the operator halted everywhere, which is
    the one direction in which this feature can lose money.

    A mode that cannot be parsed raises rather than being skipped: silently
    dropping it would answer with the weakest halt on offer.
    """
    strictest: KillSwitchMode | None = None
    for raw in modes:
        if raw is None:
            continue
        mode = raw if isinstance(raw, KillSwitchMode) else KillSwitchMode.parse(raw)
        if mode is None:
            raise ValueError(
                f"Unknown kill switch mode {raw!r}; expected one of "
                f"{', '.join(mode.value for mode in KillSwitchMode)}"
            )
        if strictest is None or mode.strictness > strictest.strictness:
            strictest = mode
    return strictest


@dataclass
class KillSwitchState:
    """The live halt state, plus the sweep ledger for as long as it is held.

    A snapshot object, refreshed in place by :meth:`refresh`: ``read_kill_switch_state``
    returns a fresh one for a one-off read, and the daemon holds one across ticks
    so the ledger survives while the halts do not. That split is the whole design
    of the throttle - a ledger in a module global would outlive the halt it
    belongs to, and a fresh ledger per read would re-sweep a flat account every
    tick.
    """

    #: ``{scope: mode}`` for every scope halted right now.
    halts: dict[str, KillSwitchMode] = field(default_factory=dict)
    #: Whether the halts above are the complete answer. ``False`` on a read
    #: failure or a stored mode that cannot be parsed.
    readable: bool = True
    #: What made the state unreadable, for the log line and the API payload.
    error: str | None = None
    #: ``(account_id, mode)`` of the last completed sweep, by monotonic clock.
    swept_at: dict[tuple[str, KillSwitchMode], float] = field(default_factory=dict)
    #: Consecutive sweeps that ended with ids still ``pending_cancel``, by
    #: account - the kill switch's ``_PENDING_CANCEL_PASSES``. Lives here for
    #: the same reason the sweep ledger does: it must survive a refresh but not
    #: a lifted halt.
    pending_cancel_passes: dict[str, int] = field(default_factory=dict)

    def refresh(self) -> KillSwitchState:
        """Re-read the database in place, keeping the sweep ledger. Returns self."""
        snapshot = read_kill_switch_state()
        self.halts = snapshot.halts
        self.readable = snapshot.readable
        self.error = snapshot.error
        # A scope the halt no longer covers has no sweep worth remembering: the
        # entry is dropped, so a re-halt sweeps at once instead of waiting out
        # the interval of a halt that has already been lifted. The same sweep of
        # a *re-armed* mode is also keyed on the mode, so an escalation never
        # waits either. The pending-cancel count is per episode for the same
        # reason: a lifted and re-armed halt starts its escalation over.
        for key in [key for key in self.swept_at if self.mode_for_account(key[0]) is None]:
            del self.swept_at[key]
        for account_id in [
            account
            for account in self.pending_cancel_passes
            if self.mode_for_account(account) is None
        ]:
            del self.pending_cancel_passes[account_id]
        return self

    def mode_for_account(self, account_id: str) -> KillSwitchMode | None:
        """The mode in force for ``account_id``, or ``None`` when it may trade.

        An unreadable state is a halt, not a green light: it answers ``entries``
        for every account, which refuses new entries without authorising any
        broker sweep.
        """
        if not self.readable:
            return KillSwitchMode.ENTRIES
        return strictest_kill_switch_mode(
            (
                self.halts.get(KILL_SWITCH_GLOBAL_SCOPE),
                self.halts.get(str(account_id)),
            )
        )

    def halted_scopes(self) -> tuple[str, ...]:
        """Every scope halted right now, sorted, global included."""
        return tuple(sorted(self.halts))

    def halted_accounts(self) -> tuple[str, ...]:
        """Every account scope halted right now, sorted (the global one omitted)."""
        return tuple(
            scope
            for scope in sorted(self.halts)
            if scope != KILL_SWITCH_GLOBAL_SCOPE
        )

    def sweep_is_due(self, account_id: str, mode: KillSwitchMode) -> bool:
        """Whether ``account_id`` may be swept in ``mode`` again.

        The interval bounds how long a broker-side order placed by hand can
        survive: refusing entries stops the daemon opening risk, not a human at
        the broker. A clear and a re-halt of the same mode inside one tick are
        indistinguishable here, and that re-halt waits out the remainder - the
        same bound the interval already sets, never longer than it.
        """
        last = self.swept_at.get((str(account_id), mode))
        if last is None:
            return True
        return (monotonic() - last) >= KILL_SWITCH_SWEEP_INTERVAL_SECONDS

    def record_sweep(self, account_id: str, mode: KillSwitchMode) -> None:
        """Remember that ``account_id`` was just swept clean in ``mode``."""
        self.swept_at[(str(account_id), mode)] = monotonic()

def read_kill_switch_state() -> KillSwitchState:
    """Read every active halt from the database, failing closed.

    One query on the daemon's hot paths. A read failure, or a stored mode that
    cannot be interpreted, comes back as an unreadable state rather than as an
    empty one: an empty answer is indistinguishable from "nothing is halted",
    which is the answer a kill switch must never produce by accident.
    """
    try:
        scopes = read_live_kill_switch_scopes()
    except Exception as exc:  # except-kind: money-decision
        # Logged with the exception in the message, not only in ``extra``: the
        # log line is the operator's only clue why every entry was refused.
        logger.error(
            "Kill switch state is unreadable (%r); treating every account as "
            "halted in 'entries' mode until it can be read again",
            exc,
        )
        return KillSwitchState(halts={}, readable=False, error=repr(exc))

    halts: dict[str, KillSwitchMode] = {}
    unreadable_modes: list[str] = []
    for scope, raw_mode in sorted(scopes.items()):
        mode = KillSwitchMode.parse(raw_mode)
        if mode is None:
            # The write path refuses an unknown mode, so this row was hand-edited
            # or written by a bug elsewhere. It is a halt (fail closed), never a
            # green light, and it must not be guessed into a destructive sweep.
            unreadable_modes.append(f"{scope}={raw_mode!r}")
            continue
        halts[str(scope)] = mode

    if unreadable_modes:
        error = "uninterpretable stored mode(s): " + ", ".join(unreadable_modes)
        logger.error(
            "Kill switch state holds a mode this build cannot interpret (%s); "
            "treating every account as halted in 'entries' mode until the row is "
            "corrected",
            error,
        )
        return KillSwitchState(halts=halts, readable=False, error=error)
    return KillSwitchState(halts=halts, readable=True)


def _halt_refusal_message(
    account_id: str,
    *,
    mode: KillSwitchMode,
    state: KillSwitchState,
    deployment_id: str | None,
    symbol: str | None,
) -> str:
    """Why this entry is refused, naming everything an operator needs to act."""
    where = f"account '{account_id}'"
    if deployment_id:
        where += f" (deployment {deployment_id}"
        where += f", symbol {symbol})" if symbol else ")"
    if not state.readable:
        return (
            f"Refusing a live entry: the kill switch state is unreadable "
            f"({state.error}), so {where} is treated as halted. An unreadable "
            "state is never read as 'nothing is halted'."
        )
    halted_scopes = [
        scope
        for scope in (KILL_SWITCH_GLOBAL_SCOPE, account_id)
        if scope in state.halts
    ]
    scope_note = (
        " the global halt covers this account"
        if halted_scopes == [KILL_SWITCH_GLOBAL_SCOPE]
        else ""
    )
    return (
        f"Refusing a live entry: {where} is halted in '{mode.value}' "
        f"mode{scope_note}. New entries are refused until the halt is lifted; "
        "open positions and working orders are untouched by this refusal."
    )


async def refuse_entry_while_halted(
    account_id: str,
    *,
    deployment_id: str | None = None,
    symbol: str | None = None,
    state: KillSwitchState | None = None,
) -> None:
    """Refuse an entry while its account is halted, and return when it is not.

    Read from the database on every call, never from a process cache: the halt
    has to hold the moment an operator writes it, and the moment a daemon starts.
    Injected ``state`` exists for a caller that already holds one (the daemon's
    tick); passing it skips the read.
    """
    resolved = state if state is not None else await asyncio.to_thread(read_kill_switch_state)
    if resolved.readable:
        mode = resolved.mode_for_account(account_id)
        if mode is None:
            return
    else:
        # Fail closed, and only that far: an unreadable state refuses entries and
        # authorises no broker sweep (see :func:`enforce_live_kill_switch`).
        mode = KillSwitchMode.ENTRIES
    raise LiveHaltError(
        _halt_refusal_message(
            account_id,
            mode=mode,
            state=resolved,
            deployment_id=deployment_id,
            symbol=symbol,
        ),
        account_id=str(account_id),
        mode=mode,
        state_unreadable=not resolved.readable,
    )


class _SweepStep(Enum):
    """How one control-plane sweep of a halted account ended."""

    COMPLETE = "complete"
    #: The adapter cannot do it at all. Not silently: the caller reports the
    #: account as unmet, so an unswept halt is visible rather than clean-looking.
    UNSUPPORTED = "unsupported"
    #: It ran and fell short - refused, partial, or it raised.
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class KillSwitchEnforcementReport:
    """What one tick's halt enforcement actually achieved, per account."""

    #: Accounts swept clean in the mode in force for them.
    swept: tuple[str, ...] = ()
    #: Accounts whose sweep fell short, so the next tick must try again.
    unmet: tuple[str, ...] = ()
    #: Halted accounts with no connected adapter, so nothing could be enforced.
    without_adapter: tuple[str, ...] = ()
    #: ``{account_id: mode}`` for every account the halt reached this tick.
    modes: Mapping[str, KillSwitchMode] = field(default_factory=dict)

    def describe(self) -> str:
        """One line for the tick's log field."""
        if not self.modes:
            return "no account halted"
        return (
            "halted "
            + ", ".join(f"{account}={mode.value}" for account, mode in sorted(self.modes.items()))
            + f"; swept={list(self.swept)} unmet={list(self.unmet)} "
            + f"no_adapter={list(self.without_adapter)}"
        )


async def _cancel_working_orders(
    account_id: str, adapter: BaseLiveAdapter, state: KillSwitchState
) -> _SweepStep:
    """Cancel every working order, reusing the STOPPED enforcement's call."""
    if not adapter.supports_cancel_orders:
        logger.error(
            "Adapter cannot cancel open orders; the halted account may still "
            "have orders resting",
            extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
        )
        return _SweepStep.UNSUPPORTED
    try:
        cancelled = await adapter.cancel_open_orders()
    except NotImplementedError as exc:
        logger.error(
            "Adapter cannot cancel open orders (%r); the halted account may "
            "still have orders resting",
            exc,
            extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
        )
        return _SweepStep.UNSUPPORTED
    except Exception as exc:  # except-kind: money-decision
        logger.error(
            "Failed to cancel the working orders of a halted account; retrying "
            "next tick (%r)",
            exc,
            extra={"data": {"account_id": account_id}},
        )
        return _SweepStep.INCOMPLETE
    if cancelled.pending_cancel:
        # ``complete`` ignores ``pending_cancel``, and a pending cancel is the one
        # outcome that can still open a position: the order may fill in a
        # cancel/fill race, so the sweep is not clean until the ids are gone. It
        # usually settles within seconds, so - exactly like the STOPPED
        # machinery's ``_PENDING_CANCEL_PASSES`` - the first passes warn and only
        # a stuck cancel escalates to ERROR. The flatten below still runs -
        # closing the positions is the money-safety action, and the next pass
        # re-checks what is left.
        passes = state.pending_cancel_passes.get(account_id, 0) + 1
        state.pending_cancel_passes[account_id] = passes
        log = (
            logger.error
            if passes >= PENDING_CANCEL_ESCALATION_PASSES
            else logger.warning
        )
        log(
            "Order cancel is not settled for a halted account; retrying next "
            "tick (pass %d): %s pending_cancel=%s",
            passes,
            cancelled.describe(),
            sorted(cancelled.pending_cancel),
            extra={
                "data": {
                    "account_id": account_id,
                    "pending_cancel": sorted(cancelled.pending_cancel),
                    "passes": passes,
                }
            },
        )
        return _SweepStep.INCOMPLETE
    if not cancelled.complete:
        # A cancel that failed outright is not a pending cancel: it logs ERROR
        # every pass, and the escalation count is left alone - the next pending
        # cancel resumes it rather than restarting, exactly like the STOPPED
        # machinery, which only clears its count on a clean pass.
        logger.error(
            "Order cancel was incomplete for a halted account; retrying next "
            "tick: %s",
            cancelled.describe(),
            extra={"data": {"account_id": account_id}},
        )
        return _SweepStep.INCOMPLETE
    state.pending_cancel_passes.pop(account_id, None)
    return _SweepStep.COMPLETE


async def _flatten_positions(account_id: str, adapter: BaseLiveAdapter) -> _SweepStep:
    """Close every open position, reusing the STOPPED enforcement's call."""
    if not adapter.supports_flatten:
        logger.error(
            "Adapter cannot flatten positions; the halted account is NOT flat",
            extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
        )
        return _SweepStep.UNSUPPORTED
    try:
        flattened = await adapter.close_all_positions()
    except NotImplementedError as exc:
        logger.error(
            "Adapter cannot flatten positions (%r); the halted account is NOT flat",
            exc,
            extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
        )
        return _SweepStep.UNSUPPORTED
    except Exception as exc:  # except-kind: money-decision
        logger.error(
            "Failed to flatten the positions of a halted account; retrying next "
            "tick (%r)",
            exc,
            extra={"data": {"account_id": account_id}},
        )
        return _SweepStep.INCOMPLETE
    if not flattened.complete:
        logger.error(
            "Flatten was incomplete for a halted account; retrying next tick: %s",
            flattened.describe(),
            extra={"data": {"account_id": account_id}},
        )
        return _SweepStep.INCOMPLETE
    return _SweepStep.COMPLETE


async def _sweep_orphaned_protections(account_id: str, adapter: BaseLiveAdapter) -> _SweepStep:
    """Cancel the protections the flatten orphaned (#274's rule, unchanged).

    A stop that outlived its position can still trigger, and a triggered
    sell-stop with nothing to sell opens a short. An adapter that cannot tell an
    orphan from a live position's stop may still count as swept - that is the
    STOPPED enforcement's rule - but one whose sweep *ran* and fell short may
    not.
    """
    try:
        orphaned = await adapter.cancel_orphaned_protections()
    except NotImplementedError:
        logger.warning(
            "Adapter cannot identify orphaned protections; the halted account is "
            "flat but protection orders may still be listed by the broker",
            extra={"data": {"account_id": account_id, "adapter": type(adapter).__name__}},
        )
        return _SweepStep.UNSUPPORTED
    except Exception as exc:  # except-kind: money-decision
        logger.error(
            "Failed to sweep the protections the flatten orphaned; the halted "
            "account is flat but a resting protection could still trigger (%r)",
            exc,
            extra={"data": {"account_id": account_id}},
        )
        return _SweepStep.INCOMPLETE
    if not orphaned.complete:
        logger.error(
            "Orphaned-protection sweep was incomplete for a halted account; "
            "retrying next tick: %s",
            orphaned.describe(),
            extra={"data": {"account_id": account_id}},
        )
        return _SweepStep.INCOMPLETE
    return _SweepStep.COMPLETE


async def _sweep_halted_account(
    account_id: str,
    adapter: BaseLiveAdapter,
    mode: KillSwitchMode,
    state: KillSwitchState,
) -> bool:
    """Run the sweeps ``mode`` calls for. ``True`` when the account is swept clean.

    The control-plane sequence is the one STOPPED enforcement runs, in the same
    order, so the two paths cannot disagree about what "cancelled and flat"
    means: cancel working orders, close positions, sweep the protections the
    flatten orphaned.

    Never raises. One account's broker failure must not abort the tick for the
    others, and an unfinished sweep is reported as unmet so the next tick retries.
    """
    cancel_step = _SweepStep.COMPLETE
    flatten_step = _SweepStep.COMPLETE
    orphan_step = _SweepStep.COMPLETE
    if mode.cancels_orders:
        cancel_step = await _cancel_working_orders(account_id, adapter, state)
    if mode.flattens:
        # Leaving positions open is worse than leaving an order resting, so a
        # cancel that fell short does not stop the flatten (#274's rule).
        flatten_step = await _flatten_positions(account_id, adapter)
        orphan_step = await _sweep_orphaned_protections(account_id, adapter)
    swept = (
        cancel_step is _SweepStep.COMPLETE
        and flatten_step is _SweepStep.COMPLETE
        and orphan_step in (_SweepStep.COMPLETE, _SweepStep.UNSUPPORTED)
    )
    if swept:
        # The message names what the mode actually did: an 'orders' halt leaves the
        # positions open, and claiming a flatten that never ran is how an operator
        # ends up believing an account is flat when it is not.
        achievement = (
            "orders cancelled and positions flattened"
            if mode.flattens
            else "working orders cancelled"
        )
        logger.warning(
            f"Enforced a halted account: {achievement}",
            extra={
                "data": {
                    "account_id": account_id,
                    "mode": mode.value,
                    "cancel": cancel_step.value,
                    "flatten": flatten_step.value,
                    "orphan_sweep": orphan_step.value,
                }
            },
        )
    else:
        logger.error(
            "Halt not fully enforced on this account; retrying next tick "
            "(cancel=%s, flatten=%s, orphaned_protections=%s)",
            cancel_step.value,
            flatten_step.value,
            orphan_step.value,
            extra={"data": {"account_id": account_id, "mode": mode.value}},
        )
    return swept


async def enforce_live_kill_switch(
    adapters: Mapping[str, BaseLiveAdapter],
    account_ids: Iterable[str] | None = None,
    *,
    state: KillSwitchState | None = None,
) -> KillSwitchEnforcementReport:
    """Enforce every halt that needs the broker, once per tick.

    ``account_ids`` is the set of accounts this caller is responsible for; with
    ``None`` it is every account the state names plus every connected adapter.
    An account in that set whose halt needs no broker action (``entries``) is
    left alone entirely - that mode is enforced at the entry gate, and an
    adapter call for it would only put load on a broker for nothing.

    An unreadable state sweeps nothing: it refuses entries (fail closed) and
    leaves the broker untouched, because liquidating on a read failure is a money
    event the operator never asked for. The halt it could not read is still
    persisted, so the next readable tick enforces it.
    """
    resolved = state if state is not None else await asyncio.to_thread(read_kill_switch_state)
    if not resolved.readable:
        logger.error(
            "Kill switch state is unreadable (%s); entries are refused and no "
            "halt sweep is attempted against any broker",
            resolved.error,
        )
        return KillSwitchEnforcementReport()

    if account_ids is None:
        candidates = set(resolved.halted_accounts()) | {str(key) for key in adapters}
    else:
        candidates = {str(account_id) for account_id in account_ids}

    swept: list[str] = []
    unmet: list[str] = []
    without_adapter: list[str] = []
    modes: dict[str, KillSwitchMode] = {}
    for account_id in sorted(candidates):
        mode = resolved.mode_for_account(account_id)
        if mode is None or mode is KillSwitchMode.ENTRIES:
            continue
        modes[account_id] = mode
        adapter = adapters.get(account_id)
        if adapter is None:
            # The account is retained for adapter connection while it is halted
            # (the sync loop connects it), so this is a connect that has not
            # landed yet - or has failed. Silence here would read as "halted and
            # flat", which is the one claim a kill switch must not make loosely.
            without_adapter.append(account_id)
            logger.error(
                "Account %s is halted in '%s' mode with no connected adapter, so "
                "the halt cannot be enforced against the broker; its entries are "
                "still refused",
                account_id,
                mode.value,
                extra={"data": {"account_id": account_id, "mode": mode.value}},
            )
            continue
        if not resolved.sweep_is_due(account_id, mode):
            continue
        if await _sweep_halted_account(account_id, adapter, mode, resolved):
            resolved.record_sweep(account_id, mode)
            swept.append(account_id)
        else:
            unmet.append(account_id)
    return KillSwitchEnforcementReport(
        swept=tuple(swept),
        unmet=tuple(unmet),
        without_adapter=tuple(without_adapter),
        modes=modes,
    )


def kill_switch_payload(state: KillSwitchState) -> dict[str, Any]:
    """The halt state as an API payload, readable exactly as it is enforced.

    ``readable: false`` is the fail-closed case and is stated rather than implied
    by an empty list of halts, because an empty list is what an unreadable state
    must never look like.
    """
    return {
        "readable": state.readable,
        "unreadable_reason": state.error,
        "global_scope": KILL_SWITCH_GLOBAL_SCOPE,
        "modes": [mode.value for mode in KillSwitchMode],
        "halts": [
            {"scope": scope, "mode": mode.value}
            for scope, mode in sorted(state.halts.items())
        ],
        "halted_accounts": list(state.halted_accounts()),
        "global_halted": KILL_SWITCH_GLOBAL_SCOPE in state.halts,
    }
