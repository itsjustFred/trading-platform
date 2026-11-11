"""Live trailing stops (issue #167).

For every open broker position that was opened by a signal carrying a
``percent`` trailing config, the protective stop is recomputed on each newly
closed bar of the deployment's signal timeframe and the broker-side stop order
is amended.

Design, in the order the guarantees depend on each other:

* **One arithmetic.** The new stop comes from
  ``RiskEngine.apply_trailing_stop`` — the same call the replay backtest makes
  per bar close — so backtest and live trail identically on the same closes
  (up to the broker's price tick, which is rounded away from the market).
* **No trailing state of its own that matters.** The current stop is read from
  the broker on every pass and the trailing config from the persisted entry
  signal (``live_signals.metadata``). A restarted daemon therefore continues
  from the broker's stop, and because the engine only ever moves a stop in the
  favourable direction, no stop can move backwards — before or after a restart.
  What lives in memory (last processed bar, retry backoff) is an optimisation
  and is dropped when the position is gone.
* **The old stop stays until the new one is confirmed.** Only adapters that
  declare ``supports_stop_amendment`` (atomic amend/replace) are trailed. A
  refused or failed amendment is logged, reported to Sentry and retried with
  backoff; the previous broker-side stop is untouched meanwhile.
* **Trigger.** Work happens once per closed bar: an account is visited when a
  bar boundary of its fastest ACTIVE deployment timeframe has passed (or a
  retry is due). Between boundaries the loop only compares timestamps — no
  broker or database I/O.

Scope limits (deliberate, documented in ``docs/live-deploy-runbook.md``):

* Only ACTIVE deployments are trailed. PAUSED leaves orders untouched and
  STOPPED flattens; in both cases the stop stays at its last broker level.
* A position is attributed to the newest *filled entry* signal for its symbol
  among the account's ACTIVE deployments. Exactly one amendable protective
  stop per symbol is required; anything else is reported and left alone.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from live_runner.base_adapter import BaseLiveAdapter, ProtectiveStop
from live_runner.signal_sources.execution_timing import _timeframe_seconds
from risk import RiskEngine, TradeSignal
from shared.database import find_latest_filled_live_entry_signals
from shared.logging import get_logger
from shared.models import LiveDeploymentConfig
from shared.sentry import capture_exception
from shared.trailing import (
    TRAILING_MODE_NONE,
    TRAILING_SL_CONFIG_METADATA_KEY,
    live_supported_trailing_modes,
    validate_trailing_sl_config,
)

logger = get_logger("live-runner")

#: How often the loop wakes to compare timestamps. Not a polling rate for the
#: broker: I/O only happens when a bar boundary has passed or a retry is due.
TRAILING_TICK_SECONDS = 15
#: Bars requested per pass. Bounds how far back a restarted daemon can catch
#: up on missed closes; older peaks are already reflected in the broker stop
#: or are lost conservatively (the stop is then lower, never higher/backwards).
TRAILING_BAR_LOOKBACK = 50
#: Retry backoff after a failed pass or a bar that is not published yet.
TRAILING_RETRY_INITIAL_SECONDS = 15.0
TRAILING_RETRY_MAX_SECONDS = 300.0

EntrySignalLoader = Callable[[list[str], list[str]], dict[str, dict[str, Any]]]


def adapter_live_trailing_modes(adapter: Any) -> frozenset[str]:
    """Trailing modes the live path executes through this adapter instance.

    The per-broker table in ``shared.trailing`` AND the adapter's own
    ``supports_stop_amendment`` capability must both allow a mode. ``None``, an
    unknown broker, a missing flag or a flag that is not literally ``True``
    all resolve to mode ``none`` only.
    """
    if adapter is None:
        return live_supported_trailing_modes(None, supports_stop_amendment=False)
    try:
        broker = getattr(adapter, "broker_name", "")
        can_amend = getattr(adapter, "supports_stop_amendment", False) is True
    except Exception:
        return live_supported_trailing_modes(None, supports_stop_amendment=False)
    return live_supported_trailing_modes(broker, supports_stop_amendment=can_amend)


@dataclass
class _Trail:
    """In-memory progress of one trailed position. Safe to lose."""

    signal_id: str
    last_bar_time: datetime | None = None
    consecutive_failures: int = 0


@dataclass
class _AccountSchedule:
    last_slot: int | None = None
    retry_at: datetime | None = None
    retry_delay: float = TRAILING_RETRY_INITIAL_SECONDS


class _TrailingFailure(Exception):
    """A pass over one position could not complete; it is surfaced and retried."""


def _normalize_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _signal_timeframe(deployment: LiveDeploymentConfig) -> str:
    configured = (deployment.signal_config or {}).get("timeframe")
    return str(configured or deployment.timeframe or "").strip()


def _position_qty(position: Mapping[str, Any]) -> float:
    if position.get("qty_unparseable"):
        raise _TrailingFailure(
            f"broker position qty is unparseable ({position.get('qty_raw')!r})"
        )
    try:
        qty = float(position.get("qty") or 0.0)
    except (TypeError, ValueError) as exc:
        raise _TrailingFailure(
            f"broker position qty is unparseable ({position.get('qty')!r})"
        ) from exc
    if not math.isfinite(qty):
        raise _TrailingFailure(f"broker position qty is not finite ({qty!r})")
    return qty


class LiveTrailingStopManager:
    """Trails broker-side stops for the open positions of the given adapters."""

    def __init__(
        self,
        *,
        load_entry_signals: EntrySignalLoader = find_latest_filled_live_entry_signals,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self._load_entry_signals = load_entry_signals
        self._risk_engine = risk_engine or RiskEngine()
        self._trails: dict[tuple[str, str], _Trail] = {}
        self._schedules: dict[str, _AccountSchedule] = {}

    # ── introspection (tests, diagnostics) ───────────────────────────────

    def tracked_positions(self) -> set[tuple[str, str]]:
        """``(account_id, SYMBOL)`` pairs with in-memory trailing progress."""
        return set(self._trails)

    # ── scheduling ───────────────────────────────────────────────────────

    async def run_once(
        self,
        adapters: Mapping[str, BaseLiveAdapter],
        deployments: Iterable[LiveDeploymentConfig],
        *,
        now: datetime | None = None,
    ) -> None:
        """Visit every account whose next closed bar (or retry) is due.

        ``adapters`` must already be filtered to adapters that are allowed to
        trade (connected, demo/paper); this method additionally requires the
        stop-amendment capability.
        """
        moment = now or datetime.now(timezone.utc)
        active_by_account: dict[str, list[LiveDeploymentConfig]] = {}
        for deployment in deployments:
            if str(deployment.status or "").strip().upper() == "ACTIVE":
                active_by_account.setdefault(deployment.account_id, []).append(deployment)

        eligible = {
            account_id: adapter
            for account_id, adapter in adapters.items()
            if account_id in active_by_account
            and getattr(adapter, "supports_stop_amendment", False) is True
        }
        # Accounts that left the eligible set keep no state: their positions
        # are no longer ours to trail, and a later return starts from the
        # broker's stop anyway.
        for account_id in set(self._schedules) - set(eligible):
            self._forget_account(account_id)

        for account_id, adapter in eligible.items():
            account_deployments = active_by_account[account_id]
            schedule = self._schedules.setdefault(account_id, _AccountSchedule())
            if not self._is_due(schedule, account_deployments, moment):
                continue
            try:
                needs_retry = await self._trail_account(
                    account_id, adapter, account_deployments, moment
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._surface(
                    "Live trailing-stop pass failed for account; broker-side stops are unchanged",
                    exc,
                    {"account_id": account_id, "adapter": type(adapter).__name__},
                )
                needs_retry = True
            if needs_retry:
                schedule.retry_at = moment + timedelta(seconds=schedule.retry_delay)
                schedule.retry_delay = min(
                    schedule.retry_delay * 2.0, TRAILING_RETRY_MAX_SECONDS
                )
            else:
                schedule.retry_at = None
                schedule.retry_delay = TRAILING_RETRY_INITIAL_SECONDS

    @staticmethod
    def _is_due(
        schedule: _AccountSchedule,
        deployments: list[LiveDeploymentConfig],
        moment: datetime,
    ) -> bool:
        seconds = [
            value
            for value in (_timeframe_seconds(_signal_timeframe(item)) for item in deployments)
            if value
        ]
        # A deployment without a usable timeframe still has to be visited so
        # the problem is reported per position; a minute is the fallback pace.
        slot_seconds = min(seconds) if seconds else 60
        slot = int(moment.timestamp() // slot_seconds)
        due = schedule.last_slot != slot or (
            schedule.retry_at is not None and moment >= schedule.retry_at
        )
        if due:
            schedule.last_slot = slot
        return due

    def _forget_account(self, account_id: str) -> None:
        self._schedules.pop(account_id, None)
        for key in [key for key in self._trails if key[0] == account_id]:
            del self._trails[key]

    # ── one account ──────────────────────────────────────────────────────

    async def _trail_account(
        self,
        account_id: str,
        adapter: BaseLiveAdapter,
        deployments: list[LiveDeploymentConfig],
        moment: datetime,
    ) -> bool:
        get_positions = getattr(adapter, "get_positions", None)
        if not callable(get_positions):
            raise _TrailingFailure(
                f"{type(adapter).__name__} cannot report positions to trail"
            )
        positions: dict[str, Mapping[str, Any]] = {}
        for row in await get_positions():
            symbol = _normalize_symbol(row.get("symbol"))
            if symbol:
                positions[symbol] = row

        # Cleanup: a position that is gone (stop/TP hit, exit signal, manual
        # close) has nothing left to trail.
        for key in [k for k in self._trails if k[0] == account_id and k[1] not in positions]:
            del self._trails[key]
        if not positions:
            return False

        deployments_by_id = {str(item.id): item for item in deployments}
        entry_signals = await asyncio.to_thread(
            self._load_entry_signals, sorted(deployments_by_id), sorted(positions)
        )

        needs_retry = False
        stops: list[ProtectiveStop] | None = None
        for symbol, position in positions.items():
            key = (account_id, symbol)
            entry = entry_signals.get(symbol)
            context = {
                "account_id": account_id,
                "symbol": symbol,
                "adapter": type(adapter).__name__,
                "signal_id": (entry or {}).get("signal_id"),
                "deployment_id": (entry or {}).get("deployment_id"),
            }
            try:
                trailing_cfg = self._trailing_config(adapter, entry, position, context)
                if trailing_cfg is None:
                    self._trails.pop(key, None)
                    continue
                assert entry is not None
                if stops is None:
                    stops = list(await adapter.list_protective_stops())
                needs_retry |= await self._trail_position(
                    key=key,
                    adapter=adapter,
                    deployment=deployments_by_id[entry["deployment_id"]],
                    entry=entry,
                    trailing_cfg=trailing_cfg,
                    position=position,
                    stops=stops,
                    moment=moment,
                    context=context,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                trail = self._trails.get(key)
                if trail is not None:
                    trail.consecutive_failures += 1
                    context["consecutive_failures"] = trail.consecutive_failures
                self._surface(
                    "Live trailing stop could not be updated; the previous "
                    "broker-side stop stays in force and the update is retried",
                    exc,
                    context,
                )
                needs_retry = True
        return needs_retry

    def _trailing_config(
        self,
        adapter: BaseLiveAdapter,
        entry: Mapping[str, Any] | None,
        position: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """The trailing config to execute for this position, or ``None``.

        ``None`` means "nothing to trail": no platform entry signal, an entry
        that asked for no trailing, a flat row, or a position on the other
        side than the entry (not the position this signal opened).
        """
        if entry is None:
            return None
        trailing_cfg = (entry.get("metadata") or {}).get(TRAILING_SL_CONFIG_METADATA_KEY)
        # A persisted signal whose mode this adapter cannot execute should
        # have been refused at submission; if one exists it is reported, never
        # silently skipped.
        mode = validate_trailing_sl_config(
            trailing_cfg,
            supported=adapter_live_trailing_modes(adapter),
            context=f"live trailing for {context.get('symbol')} (signal {entry.get('signal_id')})",
        )
        if mode is None or mode == TRAILING_MODE_NONE:
            return None
        qty = _position_qty(position)
        if math.isclose(qty, 0.0, rel_tol=0.0, abs_tol=1e-12):
            return None
        entry_is_long = entry.get("action") == "buy"
        if entry_is_long != (qty > 0):
            return None
        return dict(trailing_cfg)

    # ── one position ─────────────────────────────────────────────────────

    async def _trail_position(
        self,
        *,
        key: tuple[str, str],
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        entry: Mapping[str, Any],
        trailing_cfg: dict[str, Any],
        position: Mapping[str, Any],
        stops: list[ProtectiveStop],
        moment: datetime,
        context: dict[str, Any],
    ) -> bool:
        """Trail one position. Returns whether an early retry is wanted."""
        symbol = key[1]
        signal_id = str(entry["signal_id"])
        trail = self._trails.get(key)
        if trail is None or trail.signal_id != signal_id:
            trail = _Trail(signal_id=signal_id)
            self._trails[key] = trail

        timeframe = _signal_timeframe(deployment)
        bar_seconds = _timeframe_seconds(timeframe)
        if not bar_seconds:
            raise _TrailingFailure(
                f"deployment timeframe {timeframe!r} has no bar length to trail on"
            )
        bar_length = timedelta(seconds=bar_seconds)

        action = "buy" if entry.get("action") == "buy" else "sell"
        reducing_side = "sell" if action == "buy" else "buy"
        symbol_stops = [
            stop for stop in stops if stop.symbol == symbol and stop.side == reducing_side
        ]
        if len(symbol_stops) != 1:
            raise _TrailingFailure(
                f"expected exactly one amendable protective stop for {symbol}, "
                f"found {len(symbol_stops)}; not amending any of them"
            )
        current = symbol_stops[0]
        context["stop_order_id"] = current.order_id
        context["current_stop"] = current.stop_price

        get_market_data = getattr(adapter, "get_market_data", None)
        if not callable(get_market_data):
            raise _TrailingFailure(
                f"{type(adapter).__name__} provides no market data to trail on"
            )
        frame = await get_market_data(symbol, timeframe=timeframe, limit=TRAILING_BAR_LOOKBACK)
        bars = self._new_closed_bars(
            frame,
            bar_length=bar_length,
            moment=moment,
            entered_at=entry["timestamp"],
            after=trail.last_bar_time,
        )
        # The bar that closed at the last boundary. If it is not published yet
        # (feed delay, market closed) the pass is retried with backoff instead
        # of waiting a whole bar.
        expected_open = datetime.fromtimestamp(
            (moment.timestamp() // bar_seconds) * bar_seconds, tz=timezone.utc
        ) - bar_length
        newest_seen = bars[-1][0] if bars else trail.last_bar_time
        awaiting_bar = newest_seen is None or newest_seen < expected_open
        if not bars:
            return awaiting_bar

        # Same arithmetic as the replay adapter: one engine call per bar
        # close, starting from the stop the broker currently holds.
        # The percent arithmetic does not read the entry price; the model only
        # requires a positive one, so a broker row without it falls back to
        # the first bar close rather than blocking the trail.
        try:
            entry_price = float(position.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            entry_price = 0.0
        if not math.isfinite(entry_price) or entry_price <= 0:
            entry_price = bars[0][1]
        signal = TradeSignal(
            action=action,
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=current.stop_price,
            take_profit=None,
            timeframe=timeframe,
            metadata={TRAILING_SL_CONFIG_METADATA_KEY: dict(trailing_cfg)},
        )
        for _bar_time, close in bars:
            self._risk_engine.apply_trailing_stop(signal, market_price=close)
        target = signal.stop_loss
        if target is None:
            raise _TrailingFailure("trailing produced no stop")
        target = adapter.round_stop_price(current, float(target))
        improves = (
            target > current.stop_price if reducing_side == "sell" else target < current.stop_price
        )
        if improves:
            context["target_stop"] = target
            amended = await adapter.amend_protective_stop(current, target)
            logger.info(
                "Trailed live stop",
                extra={
                    "data": {
                        **context,
                        "new_stop": amended.stop_price,
                        "new_stop_order_id": amended.order_id,
                        "bar_time": bars[-1][0].isoformat(),
                    }
                },
            )
        # Only reached when the broker confirmed the amendment (or none was
        # needed): a failed amendment leaves these bars unprocessed, so the
        # retry recomputes the same target.
        trail.last_bar_time = bars[-1][0]
        trail.consecutive_failures = 0
        return awaiting_bar

    @staticmethod
    def _new_closed_bars(
        frame: Any,
        *,
        bar_length: timedelta,
        moment: datetime,
        entered_at: datetime,
        after: datetime | None,
    ) -> list[tuple[datetime, float]]:
        """Closed bars to trail on, oldest first, as ``(open_time, close)``.

        A bar counts once it has fully closed (``open + length <= now``) and
        only if it closed after the entry signal; bars at or before ``after``
        were already applied.
        """
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise _TrailingFailure("market data returned no bars")
        if "timestamp" not in frame.columns or "close" not in frame.columns:
            raise _TrailingFailure("market data has no timestamp/close columns")
        bars: list[tuple[datetime, float]] = []
        ordered = frame.sort_values("timestamp")
        for raw_time, raw_close in zip(ordered["timestamp"], ordered["close"]):
            opened = pd.Timestamp(raw_time)
            if opened.tzinfo is None:
                opened = opened.tz_localize("UTC")
            else:
                opened = opened.tz_convert("UTC")
            open_time = opened.to_pydatetime()
            close_time = open_time + bar_length
            if close_time > moment or close_time <= entered_at:
                continue
            if after is not None and open_time <= after:
                continue
            close = float(raw_close)
            if not math.isfinite(close) or close <= 0:
                raise _TrailingFailure(
                    f"bar {open_time.isoformat()} has an unusable close ({raw_close!r})"
                )
            bars.append((open_time, close))
        return bars

    # ── failure surfacing ────────────────────────────────────────────────

    @staticmethod
    def _surface(message: str, exc: BaseException, context: Mapping[str, Any]) -> None:
        logger.error(
            message,
            extra={"data": {**context, "error": repr(exc)}},
        )
        with contextlib.suppress(Exception):
            capture_exception(
                exc,
                service_name="live-runner",
                context={"component": "live_trailing_stops", **context},
            )


async def run_trailing_stops_loop(
    manager: LiveTrailingStopManager,
    *,
    eligible_adapters: Callable[[], Mapping[str, BaseLiveAdapter]],
    current_deployments: Callable[[], Iterable[LiveDeploymentConfig]],
    interval_seconds: float = TRAILING_TICK_SECONDS,
) -> None:
    """Drive ``manager`` forever. One failed tick never ends the loop.

    Both callables are in-memory reads (the daemon's adapter map and the
    deployment snapshot its sync loop already maintains), so a tick that finds
    nothing due costs no broker or database I/O.
    """
    while True:
        try:
            await manager.run_once(eligible_adapters(), current_deployments())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LiveTrailingStopManager._surface(
                "Live trailing-stop tick failed", exc, {"phase": "tick"}
            )
        await asyncio.sleep(interval_seconds)
