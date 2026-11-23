"""Live strategy runner for ``internal`` deployments (issue #348).

Evaluates every ACTIVE deployment whose signal source is ``internal`` on newly
closed bars and emits the same signals the native backtest engine would, then
hands them to the daemon's existing risk/submit chokepoint.

Timing contract (native engine parity)
--------------------------------------
The native ``TradeEngine`` evaluates ``generate_signals`` once over the whole
frame, then walks bars in order. On the signal bar it records the bar ``close``
as the order's entry reference and creates a *pending* market order that only
fills on a later bar (``i > order.created_index``) — i.e. the **next bar's
open**. Signal exits fill at the trigger bar's ``close``
(``BacktestConfig.signal_exit_fill="close"`` default).

Live cannot know the next open in advance, so the mapping is:

* **Entries** — evaluated on the just-closed bar T, submitted immediately after
  T closes. The broker fills the market order at (or very near) the open of
  bar T+1, which is exactly the price the engine assumes. ``theoretical_price``
  and sizing geometry use bar T's ``close``, the same reference the engine
  records on the pending order.
* **Exits** — evaluated on the just-closed bar T and submitted immediately;
  the market close fills at approximately the trigger bar's close, matching
  the engine's default signal-exit fill.

A signal is only actionable while bar T+1 is still forming: the window is
``SUBMIT_GRACE_SECONDS`` (30 s) plus a small feed-lag allowance of
``min(120 s, 10% of the bar)``, measured from the bar's *session-aware*
close — never a full extra bar, so a signal cannot trade an hour late.
Older bars are stale: their entries are skipped rather than traded late.
Exits follow the same freshness rule — a stale exit is skipped because the
broker-side SL/TP legs placed at entry remain the bound on the position;
with an unbounded take-profit policy that means the position is then held
until the fallback stop or the next exit signal.

Catch-up policy: only the newest closed bar is actionable after a restart or
downtime. Bars that closed while the daemon was down are stale by definition
and are never backfilled with orders.

Signal semantics: the same strategy file is loaded through
``live_runner.promotion.strategy_target`` (the file the promotion gate binds),
parameters are ``STRATEGY_META["parameters"]`` defaults merged exactly like
``resolve_strategy_parameters(path, None)``, and the call/normalization is the
native engine's own ``TradeEngine._call_strategy`` /
``_normalize_strategy_output`` / ``_resolve_direction`` / ``_extract_levels`` —
not a reimplementation.

Deployment configuration rules: ``settings["strategy_parameters"]`` and any
``signal_processor`` configuration are refused — the internal path always runs
the strategy file with its ``STRATEGY_META`` defaults and never runs a signal
processor (processors are a Discord-pipeline concept). Bars come from the
adapter's ``get_market_data`` feed (Alpaca IEX for equities, the crypto feed
for pairs). The strategy cache is keyed by deployment id and reloaded when the
resolved file's mtime changes; helper modules the strategy imports are *not*
reloaded — restart the daemon to pick those up.

Opt-in: an internal deployment is only evaluated when
``settings["internal_runner_enabled"]`` is truthy. Existing ACTIVE internal
deployments therefore stay inert until an operator sets the flag explicitly
(maintainer decision 2026-11-22); there is no way to tell a pre-existing row
from a new one, so the flag gates every internal deployment.

Risk-policy notes (maintainer decision 2026-11-22):

* An entry with **no take-profit level** is emitted with
  ``take_profit_policy=unbounded_exit_on_opposite_signal`` — the strategy's own
  ``exits`` signal is its exit mechanism, matching the backtest.
* An entry with **no stop loss** gets a broker stop at the engine's fallback
  distance (``fallback_stop_pct`` of the entry price — the same fallback the
  engine uses for sizing). Live never runs unprotected.
* A **single take-profit with ``tp_pct``/``fraction < 1``** is refused like a
  multi-level ladder: the live bracket carries one full-size TP.
* Entries per symbol are capped by ``internal_max_open_per_symbol``
  (deployment ``settings``, falling back to a same-named ``STRATEGY_META``
  parameter): default **1 open position per deployment and symbol**. Only 1
  is accepted today — N>1 is not supported end-to-end (per-position sizing,
  protection and exits on the qty-netted live ledger are unresolved;
  maintainer decision 2026-11-22: N=1, N>1 is #382) and any other value
  fails closed. Open
  positions are counted from the deployment's own persisted ledger —
  filled residuals allocated FIFO plus unfilled entries the broker still
  lists as working or holds a non-zero position for — and the total is
  capped at ``TRADE_ENGINE_MAX_POSITIONS``.
  When the cap or the ledger cannot be read, entries are disabled for the
  deployment while exits keep flowing.
* **Session-bound symbols** (everything the venue calendar does not resolve
  to 24/7) are only *evaluated* while the venue is open, using
  ``shared.venue_calendar``. Only actionability is session-gated, never the
  indicator input: intraday bars are closed at their nominal close and the
  strategy sees every closed bar the feed serves — including extended-hours
  bars, matching the unfiltered backtest dataset. A bar *produces a signal*
  only when it overlaps a trading interval; daily-and-longer bars are allowed
  and may be held over the market close, their signals becoming actionable at
  the next session open. An intraday bar whose data runs into the session
  close is clipped to it and likewise becomes actionable at the next session
  open; a bar that ends inside the session is actionable once its close plus
  the freshness window has passed.
* Only **ACTIVE** deployments are evaluated — a PAUSED internal deployment
  gets no exits from this runner, unlike PAUSED Discord deployments which
  #311 still lets exit (documented divergence; the Discord path is the
  operator's manual-exit channel).
* A same-bar reversal (exit + new entry on one bar) emits the exit but
  skips the entry: ``open_entries`` still counts the position the exit is
  closing, so the entry waits for the next bar's evaluation — a one-bar
  delay, not a dropped signal (documented divergence).

Submission: intents are handed to the daemon's ``submit_signal`` callback,
which routes them through ``_trade_signal_to_live_signal`` and
``_prepare_claim_and_submit_signal`` — the same prepare/claim/submit path as
Discord signals, so sizing, the SL/TP guard, promotion gate, submission lease
and adapter persistence all apply unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from live_runner.base_adapter import UNBOUNDED_TP_POLICY, BaseLiveAdapter
from live_runner.exit_signals import FULL_EXIT, exit_intent_metadata
from live_runner.promotion import (
    STRATEGY_PARAMETERS_KEY,
    deployment_signal_processor_name,
    strategy_target,
)
from live_runner.signal_sources.execution_timing import _timeframe_seconds
from live_runner.symbol_class import classify_symbol
from risk.models import TradeSignal
from runner.engine_pkg.trading_engine import TRADE_ENGINE_MAX_POSITIONS, TradeEngine
from runner.engines.base import BacktestConfig, resolve_strategy_parameters
from shared import venue_calendar
from shared.constants import normalize_symbols, normalize_timeframe
from shared.database import (
    DATA_DIR,
    list_live_deployment_open_entry_counts_by_symbol,
    list_live_deployment_unfilled_entry_order_refs,
)
from shared.logging import get_logger
from shared.models import VALID_TIMEFRAMES, LiveDeploymentConfig
from shared.strategy_registry import load_strategy_module

logger = get_logger("live-runner")

#: How often the loop wakes. I/O only happens when a bar boundary has passed.
INTERNAL_RUNNER_TICK_SECONDS = 15
#: Bars requested per evaluation. Bounds indicator warmup; the adapter caps
#: the request (Alpaca: 1000). A strategy needing deeper history than the feed
#: serves is a documented limitation, not silently truncated further.
INTERNAL_RUNNER_BAR_LIMIT = 1000
#: Grace past the bar close during which a just-closed signal may still be
#: submitted. Covers feed publication delay and tick scheduling jitter; the
#: entry-deviation gate (#125) still bounds how far the fill may drift. The
#: actionable window is this grace *plus* a small feed-lag allowance — never a
#: full extra bar — so a signal is only live while bar T+1 is forming.
SUBMIT_GRACE_SECONDS = 30.0
SUBMIT_FEED_LAG_BAR_FRACTION = 0.1
SUBMIT_FEED_LAG_CAP_SECONDS = 120.0
#: Opt-in flag (deployment ``settings``): internal deployments are inert
#: until an operator sets it — maintainer decision 2026-11-22 rollout rule.
INTERNAL_RUNNER_ENABLED_KEY = "internal_runner_enabled"
#: Per-symbol open-entry cap (deployment ``settings``, falling back to a
#: same-named ``STRATEGY_META`` parameter). Only 1 is accepted: N>1 per-symbol
#: entries is not supported end-to-end yet (maintainer decision 2026-11-22:
#: N=1; N>1 is #382) and any other value fails closed.
MAX_OPEN_PER_SYMBOL_KEY = "internal_max_open_per_symbol"
#: How far ahead the venue calendar is asked to find the next session open.
SESSION_LOOKAHEAD_DAYS = 14
#: Broker position quantities at or below this are treated as flat when
#: deciding whether a symbol is held (dust, not a real position).
_BROKER_POSITION_QTY_EPSILON = 1e-9
#: Consecutive submission failures before a one-time ERROR escalation.
SUBMIT_FAILURE_ESCALATION = 3

#: Outcomes the daemon's submit chokepoint reports back through
#: ``SubmitSignal``. ``submitted`` reached the broker; ``ignored`` was a
#: deliberate no-op (e.g. an exit with nothing to close); ``refused`` was
#: rejected before the broker saw anything; ``unknown`` means the broker may
#: have the order and reconciliation is required. Anything else is treated
#: as a failure.
SUBMIT_OUTCOME_SUBMITTED = "submitted"
SUBMIT_OUTCOME_REFUSED = "refused"
SUBMIT_OUTCOME_IGNORED = "ignored"
SUBMIT_OUTCOME_UNKNOWN = "unknown"
_SUBMIT_OK_OUTCOMES = frozenset(
    {SUBMIT_OUTCOME_SUBMITTED, SUBMIT_OUTCOME_IGNORED}
)
#: Outcomes where an entry may exist at the broker, so this pass's caps must
#: count it even though no clean submission was confirmed.
_SUBMIT_MAY_HAVE_REACHED_BROKER = frozenset(
    {SUBMIT_OUTCOME_SUBMITTED, SUBMIT_OUTCOME_UNKNOWN}
)

SubmitSignal = Callable[
    [BaseLiveAdapter, LiveDeploymentConfig, TradeSignal], Awaitable[str]
]


def _deployment_is_active_internal(deployment: LiveDeploymentConfig) -> bool:
    if str(deployment.status or "").strip().upper() != "ACTIVE":
        return False
    source_type = str(deployment.signal_source_type or "internal").strip().lower()
    source = str(deployment.signal_source or "internal").strip().lower()
    return source_type == "internal" and source == "internal"


def _deployment_symbols(deployment: LiveDeploymentConfig) -> list[str]:
    """Concrete symbols to evaluate; ``*"``/blank entries fail closed."""
    symbols = normalize_symbols(deployment.symbols)
    concrete = [symbol for symbol in symbols if symbol != "*"]
    if len(concrete) != len(symbols):
        raise ValueError(
            f"internal deployment {deployment.id} has a wildcard symbol; "
            "a strategy needs concrete symbols to fetch bars for"
        )
    return concrete


def _adapter_serves_bars(adapter: BaseLiveAdapter, timeframe: str) -> bool:
    get_market_data = getattr(adapter, "get_market_data", None)
    if not callable(get_market_data):
        return False
    serves = getattr(adapter, "supports_market_data_timeframe", None)
    if callable(serves) and not serves(timeframe):
        return False
    return True


def _is_crypto_symbol(symbol: str) -> bool:
    try:
        return classify_symbol(symbol) == "crypto"
    except Exception:  # except-kind: money-decision
        # Unclassifiable symbols are treated as session-bound: refusing a
        # daily+ deployment is the fail-closed direction.
        return False


def _internal_runner_opted_in(deployment: LiveDeploymentConfig) -> bool:
    """Explicit per-deployment opt-in (maintainer decision 2026-11-22).

    ``settings["internal_runner_enabled"]`` must be an unmistakable truthy
    value; anything else — absent, false, a typo — leaves the deployment
    inert. There is no creation-time distinction between pre-existing and
    new ACTIVE internal rows, so the flag gates all of them.
    """
    value = (deployment.settings or {}).get(INTERNAL_RUNNER_ENABLED_KEY)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _max_open_per_symbol(
    deployment: LiveDeploymentConfig, bundle: _StrategyBundle
) -> int:
    """Configured per-symbol open-entry cap.

    Deployment ``settings`` win; a same-named ``STRATEGY_META`` parameter is
    the per-strategy fallback. Default 1. Only 1 is accepted: N>1 per-symbol
    entries is not supported end-to-end on the qty-netted live ledger
    (maintainer decision 2026-11-22: N=1; N>1 is #382) — anything else, or
    an unreadable value, fails closed (the caller disables entries while
    exits keep flowing).
    """
    settings = deployment.settings or {}
    if MAX_OPEN_PER_SYMBOL_KEY in settings:
        raw = settings[MAX_OPEN_PER_SYMBOL_KEY]
    else:
        raw = (bundle.engine.config.parameters or {}).get(MAX_OPEN_PER_SYMBOL_KEY, 1)
    try:
        if isinstance(raw, bool):
            raise ValueError("boolean is not a count")
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{MAX_OPEN_PER_SYMBOL_KEY}={raw!r} is not an integer"
        ) from exc
    if not number.is_integer():
        raise ValueError(
            f"{MAX_OPEN_PER_SYMBOL_KEY}={raw!r} is not an integer"
        )
    value = int(number)
    if value != 1:
        raise ValueError(
            f"{MAX_OPEN_PER_SYMBOL_KEY}={value} is not supported: only 1 open "
            "entry per deployment and symbol is implemented end-to-end "
            "(per-position sizing, protection and exits on the netted live "
            "ledger; maintainer decision 2026-11-22: N=1, N>1 is #382)"
        )
    return value


def _adapter_calendar_source(adapter: BaseLiveAdapter) -> str:
    """Broker cache namespace for venue-calendar resolution."""
    name = type(adapter).__name__.lower()
    if "alpaca" in name:
        return "alpaca"
    if "ctrader" in name:
        return "ctrader"
    return ""


def _next_session_open(
    calendar: venue_calendar.VenueCalendar, ts: pd.Timestamp
) -> pd.Timestamp | None:
    """First trading-interval open strictly after ``ts`` (``None`` = none found)."""
    ts = pd.Timestamp(ts)
    for interval in calendar.trading_intervals(
        ts, ts + pd.Timedelta(days=SESSION_LOOKAHEAD_DAYS)
    ):
        if interval.open > ts:
            return interval.open
        if interval.close > ts:
            # ``ts`` sits inside a session; the next open is a later interval.
            continue
    return None


def _effective_bar_close(
    calendar: venue_calendar.VenueCalendar,
    bar_open: pd.Timestamp,
    bar_seconds: int,
) -> tuple[pd.Timestamp, bool] | None:
    """Session-aware ``(close, hits_session_end)`` for one bar.

    ``hits_session_end`` is True when the bar's data runs into the session
    close (or the bar is daily-or-longer, whose close is always a session
    close): its signal then becomes actionable at the *next* session open,
    matching the engine's next-bar-open fill. Returns ``None`` for a bar
    that never overlaps trading time — it produces no signal.
    """
    bar_open = pd.Timestamp(bar_open)
    nominal_close = bar_open + pd.Timedelta(seconds=bar_seconds)
    # A bar counts when it *overlaps* a trading interval, not when it opens
    # inside one: clock-aligned feeds stamp e.g. the 09:00 ET 1h bar, whose
    # 09:30-10:00 ET portion is regular-hours data even though the bar opened
    # pre-market. Requiring ``bar_open`` inside the session dropped exactly
    # those bars (and every 4h bar stamped before the open) from the
    # strategy's input — a backtest-parity bug.
    intervals = calendar.trading_intervals(
        bar_open - pd.Timedelta(days=1), nominal_close + pd.Timedelta(days=1)
    )
    overlapping = [
        interval
        for interval in intervals
        if interval.open < nominal_close and interval.close > bar_open
    ]
    if not overlapping:
        return None
    session_close = overlapping[-1].close
    if bar_seconds >= 86_400:
        # Daily+: the stamped bar spans whole sessions; its effective close
        # is the last session close overlapping the span.
        return session_close, True
    effective_close = min(nominal_close, session_close)
    return effective_close, nominal_close >= session_close


def _has_sizing_only_stop_fields(row: pd.Series) -> bool:
    """True when the signal row carries engine sizing-stop hints.

    The engine uses ``sizing_sl_price``/``sizing_stop``/``sizing_sl``/
    ``stop_distance`` for position sizing only — they must never become a live
    broker stop, and the live path cannot reproduce the engine's sizing math,
    so any entry carrying them is refused.
    """
    for key in ("sizing_sl_price", "sizing_stop", "sizing_sl", "stop_distance"):
        if key in row.index and pd.notna(row.get(key)):
            return True
    return False


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Mirror ``TradeEngine.run``'s frame normalization exactly."""
    prepared = frame.copy()
    prepared.columns = [str(col).lower() for col in prepared.columns]
    if "timestamp" not in prepared.columns:
        if isinstance(prepared.index, pd.DatetimeIndex):
            prepared = prepared.reset_index().rename(columns={"index": "timestamp"})
        else:
            raise ValueError("market data frame must include a timestamp column")
    prepared["timestamp"] = pd.to_datetime(
        prepared["timestamp"], utc=True, errors="coerce"
    )
    # No sort: the engine consumes bars in feed order, and the feed contract
    # is oldest-to-newest. Reordering here would silently change semantics.
    prepared = prepared.dropna(subset=["timestamp"]).reset_index(drop=True)
    return prepared


def _bar_close_time(open_time: pd.Timestamp, bar_seconds: int) -> pd.Timestamp:
    return open_time + pd.Timedelta(seconds=bar_seconds)


def _iso(moment: pd.Timestamp | datetime) -> str:
    return moment.isoformat()


def _intent_idempotency_key(
    deployment: LiveDeploymentConfig,
    symbol: str,
    timeframe: str,
    bar_close: pd.Timestamp,
    *,
    kind: str,
) -> str:
    """Producer-stable per-bar key for one emitted intent.

    Stamped explicitly (the daemon's ``_mint_signal_idempotency_key`` defers
    to a pre-set key) so an exit and an entry emitted on the same bar can
    never collide, and so the key survives a daemon restart unchanged.
    """
    material = "|".join(
        [
            str(deployment.id),
            symbol.upper(),
            timeframe,
            _iso(bar_close),
            kind,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass
class _StrategyBundle:
    """Cached strategy module + the engine helpers that interpret its output."""

    mtime_ns: int
    path: str
    engine: TradeEngine
    generate_signals: Any


class InternalStrategyRunner:
    """Evaluates ACTIVE internal deployments on newly closed bars."""

    def __init__(
        self,
        *,
        submit_signal: SubmitSignal,
        load_target: Callable[[str], Any] = strategy_target,
        load_module: Callable[..., Any] = load_strategy_module,
        bar_limit: int = INTERNAL_RUNNER_BAR_LIMIT,
    ) -> None:
        self._submit_signal = submit_signal
        self._load_target = load_target
        self._load_module = load_module
        self._bar_limit = int(bar_limit)
        self._bundles: dict[str, _StrategyBundle] = {}
        # (deployment_id, SYMBOL) -> open time of the newest bar already acted on.
        self._last_bar: dict[tuple[str, str], pd.Timestamp] = {}
        # (deployment_id, SYMBOL) -> consecutive submission failures.
        self._submit_failures: dict[tuple[str, str], int] = {}
        # (broker source, SYMBOL) -> resolved venue calendar (None = unknown).
        self._calendars: dict[tuple[str, str], venue_calendar.VenueCalendar | None] = {}
        # Throttled one-shot logs: (key, category) already reported.
        self._reported: set[tuple[str, str]] = set()

    # ── strategy resolution ──────────────────────────────────────────────

    def _report_once(
        self,
        key: str,
        category: str,
        message: str,
        *,
        level: str = "error",
        **extra: Any,
    ) -> None:
        marker = (key, category)
        if marker in self._reported:
            return
        self._reported.add(marker)
        getattr(logger, level)(message, extra={"data": extra})

    def _note_submit_failure(
        self,
        state_key: tuple[str, str],
        symbol: str,
        signal: TradeSignal,
        *,
        message: str,
        level: str = "error",
        **detail: Any,
    ) -> None:
        """Count one non-submission (exception or refused/unknown outcome).

        Refusals return normally through the chokepoint, so without this they
        would never escalate: three consecutive signals that never reach the
        broker means the deployment is effectively not trading.
        """
        failures = self._submit_failures.get(state_key, 0) + 1
        self._submit_failures[state_key] = failures
        if failures == SUBMIT_FAILURE_ESCALATION:
            self._report_once(
                state_key[0],
                f"submit_failures:{state_key[1]}",
                "Internal strategy signal submission keeps failing; "
                "the deployment is effectively not trading",
                deployment_id=state_key[0],
                symbol=symbol,
                consecutive_failures=failures,
            )
        getattr(logger, level)(
            message,
            extra={
                "data": {
                    "deployment_id": state_key[0],
                    "symbol": symbol,
                    "action": signal.action,
                    **detail,
                }
            },
        )

    def _bundle(self, deployment: LiveDeploymentConfig, timeframe: str) -> _StrategyBundle | None:
        """Resolve + load the deployment's strategy, cached by file mtime."""
        key = str(deployment.id)
        try:
            target = self._load_target(deployment.strategy_name)
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                key,
                "strategy_target",
                "Internal deployment strategy cannot be resolved; skipping",
                deployment_id=key,
                strategy_name=deployment.strategy_name,
                error=str(exc),
            )
            return None
        path = Path(target.path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            self._report_once(
                key,
                "strategy_stat",
                "Internal deployment strategy file is unreadable; skipping",
                deployment_id=key,
                strategy_path=str(path),
                error=str(exc),
            )
            return None
        cached = self._bundles.get(key)
        if (
            cached is not None
            and cached.mtime_ns == mtime_ns
            and cached.path == str(path)
        ):
            return cached
        try:
            module = self._load_module(path, strategies_dir=target.root)
            generate_signals = getattr(module, "generate_signals", None)
            if not callable(generate_signals):
                raise ValueError("strategy module does not define generate_signals")
            parameters = resolve_strategy_parameters(path, None)
            config = BacktestConfig(
                strategy_name=deployment.strategy_name,
                symbols=[str(deployment.symbols[0] if deployment.symbols else "")],
                timeframe=timeframe,
                start_date="1970-01-01",
                end_date="2100-01-01",
                parameters=parameters,
            )
            engine_symbol = str(deployment.symbols[0] if deployment.symbols else "")
            engine = TradeEngine(
                config,
                symbol=engine_symbol,
                # #354: the slippage model refuses an undetermined asset
                # class; derive it the same way the session calendar does.
                asset_class=(
                    "crypto" if _is_crypto_symbol(engine_symbol) else "stock"
                ),
            )
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                key,
                f"strategy_load:{mtime_ns}",
                "Internal deployment strategy failed to load; skipping",
                deployment_id=key,
                strategy_path=str(path),
                error=str(exc),
            )
            return None
        bundle = _StrategyBundle(
            mtime_ns=mtime_ns,
            path=str(path),
            engine=engine,
            generate_signals=generate_signals,
        )
        self._bundles[key] = bundle
        self._reported.discard((key, "strategy_target"))
        return bundle

    async def _working_order_ids(
        self, adapter: BaseLiveAdapter, deployment: LiveDeploymentConfig
    ) -> set[str] | None:
        """The adapter's working-order ids, or ``None`` when unprovable.

        Called only when the ledger holds unfilled entries: such an entry
        counts as open only while the broker still lists its order, so a dead
        unfilled order cannot lock the symbol. ``None`` fails closed — the
        caller disables entries rather than guess which orders are dead —
        and is reported once per deployment.
        """
        deployment_id = str(deployment.id)
        getter = getattr(adapter, "working_order_ids", None)
        if not callable(getter):
            self._report_once(
                deployment_id,
                "working_orders",
                "Adapter cannot list working orders; unfilled entries cannot "
                "be told from dead ones, so entries are disabled (exits "
                "still evaluated)",
                deployment_id=deployment_id,
                adapter=type(adapter).__name__,
            )
            return None
        try:
            return {str(order_id) for order_id in await getter()}
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                deployment_id,
                "working_orders",
                "Broker working orders could not be read; unfilled entries "
                "cannot be told from dead ones, so entries are disabled "
                "(exits still evaluated)",
                deployment_id=deployment_id,
                error=str(exc),
            )
            return None

    async def _broker_held_symbols(
        self, adapter: BaseLiveAdapter, deployment: LiveDeploymentConfig
    ) -> set[str] | None:
        """Symbols where the broker reports a non-zero position (#360).

        A filled order is not working, so the working-order list cannot tell
        a filled-but-unbooked entry from a dead unfilled one; the account's
        open positions can. Called only when unfilled entries exist that the
        working list does not cover. ``None`` fails closed — the caller
        disables entries rather than guess — and is reported once per
        deployment. Over-counts when another deployment holds the symbol:
        that only blocks entries.
        """
        deployment_id = str(deployment.id)
        getter = getattr(adapter, "get_positions", None)
        if not callable(getter):
            self._report_once(
                deployment_id,
                "broker_positions",
                "Adapter cannot list positions; filled-but-unbooked entries "
                "cannot be told from dead ones, so entries are disabled "
                "(exits still evaluated)",
                deployment_id=deployment_id,
                adapter=type(adapter).__name__,
            )
            return None
        try:
            positions = await getter()
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                deployment_id,
                "broker_positions",
                "Broker positions could not be read; filled-but-unbooked "
                "entries cannot be told from dead ones, so entries are "
                "disabled (exits still evaluated)",
                deployment_id=deployment_id,
                error=str(exc),
            )
            return None
        if positions is None:
            # ``None`` is "the adapter could not prove the book", not "flat":
            # guessing flat here would read a real position as a dead order.
            self._report_once(
                deployment_id,
                "broker_positions",
                "Broker positions read returned nothing; filled-but-unbooked "
                "entries cannot be told from dead ones, so entries are "
                "disabled (exits still evaluated)",
                deployment_id=deployment_id,
                adapter=type(adapter).__name__,
            )
            return None
        held: set[str] = set()
        for position in positions:
            if not isinstance(position, dict):
                self._report_once(
                    deployment_id,
                    "broker_positions",
                    "Broker positions read returned a non-dict row; "
                    "filled-but-unbooked entries cannot be told from dead "
                    "ones, so entries are disabled (exits still evaluated)",
                    deployment_id=deployment_id,
                    adapter=type(adapter).__name__,
                )
                return None
            row = position
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            qty = row.get("qty")
            try:
                qty_value = abs(float(qty)) if qty is not None else 0.0
            except (TypeError, ValueError):
                qty_value = float("inf")
            # An unparseable or non-finite qty, or the adapter's explicit
            # unreadable flag, counts as held: guessing "flat" here would
            # read a real position as a dead order.
            if (
                row.get("qty_unparseable")
                or not qty_value < float("inf")
                or qty_value > _BROKER_POSITION_QTY_EPSILON
            ):
                held.add(symbol)
        return held

    def _symbol_calendar(
        self, adapter: BaseLiveAdapter, symbol: str
    ) -> venue_calendar.VenueCalendar | None:
        """Venue calendar for session checks; ``None`` = venue unknown.

        Only successes are cached: a transient resolution failure must not
        disable the symbol until a restart — the next tick retries.
        """
        source = _adapter_calendar_source(adapter)
        key = (source, symbol.upper())
        if key in self._calendars:
            return self._calendars[key]
        asset_class = "crypto" if _is_crypto_symbol(symbol) else "stock"
        try:
            calendar = venue_calendar.resolve_venue_calendar(
                symbol=symbol,
                asset_class=asset_class,
                source=source or None,
                data_dir=str(DATA_DIR),
            )
        except Exception:  # except-kind: money-decision
            calendar = None
        if calendar is not None:
            self._calendars[key] = calendar
        return calendar

    # ── per-symbol evaluation ────────────────────────────────────────────

    async def _evaluate_symbol(
        self,
        *,
        adapter: BaseLiveAdapter,
        deployment: LiveDeploymentConfig,
        bundle: _StrategyBundle,
        symbol: str,
        timeframe: str,
        bar_seconds: int,
        moment: datetime,
        open_entries: dict[str, int],
        max_open_per_symbol: int,
    ) -> None:
        state_key = (str(deployment.id), symbol)
        calendar = self._symbol_calendar(adapter, symbol)
        if calendar is None:
            # The venue cannot be resolved: it may be session-bound, so
            # evaluating bars blindly is not safe. Fail closed per symbol.
            self._report_once(
                state_key[0],
                f"calendar:{state_key[1]}",
                "Internal strategy symbol has no resolvable venue calendar; "
                "session timing cannot be proven and evaluation fails closed",
                deployment_id=state_key[0],
                symbol=symbol,
            )
            return
        session_bound = not isinstance(calendar, venue_calendar.ContinuousCalendar)
        moment_ts = pd.Timestamp(moment)
        if session_bound:
            state = calendar.session_state(moment_ts)
            if state == "closed":
                # Nothing is actionable while the venue is shut; bars that
                # closed are re-evaluated at the next session open, when
                # their entries can actually fill.
                return
            if state != "open":
                self._report_once(
                    state_key[0],
                    f"calendar_unknown:{state_key[1]}",
                    "Internal strategy venue calendar cannot speak for now; "
                    "evaluation fails closed",
                    deployment_id=state_key[0],
                    symbol=symbol,
                    session_state=state,
                )
                return
        last_processed = self._last_bar.get(state_key)
        if last_processed is not None:
            # The newest bar that could carry an actionable signal opens at
            # last_processed + bar_seconds and closes one bar later. Until
            # then there is nothing new to evaluate, so skip the 1000-bar
            # fetch entirely rather than polling it every tick.
            nominal_next_close = last_processed + pd.Timedelta(
                seconds=2 * bar_seconds
            )
            if session_bound:
                # Bar grids are not contiguous across a session close: the
                # next close may fall in closed time, in which case the next
                # actionable moment is the following session open.
                wake = (
                    nominal_next_close
                    if calendar.is_trading_instant(nominal_next_close)
                    else _next_session_open(calendar, nominal_next_close)
                )
                if wake is not None and moment_ts < wake:
                    return
            elif moment_ts < nominal_next_close:
                return
        # ``get_market_data`` is a duck-typed adapter capability (not on
        # ``BaseLiveAdapter``), resolved the same way ``_adapter_serves_bars``
        # and the trailing-stop manager probe it.
        get_market_data = getattr(adapter, "get_market_data", None)
        if not callable(get_market_data):
            self._report_once(
                state_key[0],
                f"market_data:{state_key[1]}",
                "Adapter cannot serve market data for this timeframe; "
                "internal strategy evaluation fails closed",
                deployment_id=state_key[0],
                symbol=symbol,
            )
            return
        try:
            frame = await get_market_data(
                symbol, timeframe=timeframe, limit=self._bar_limit
            )
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                state_key[0],
                f"market_data:{state_key[1]}",
                "Internal strategy market data fetch failed; skipping symbol",
                deployment_id=state_key[0],
                symbol=symbol,
                error=str(exc),
            )
            return
        try:
            prepared = _prepare_frame(frame)
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                state_key[0],
                f"frame:{state_key[1]}",
                "Internal strategy market data frame is unusable; skipping symbol",
                deployment_id=state_key[0],
                symbol=symbol,
                error=str(exc),
            )
            return
        if prepared.empty:
            return

        if session_bound:
            # Daily+ bars are closed once their *session-aware* close has
            # passed — the session close, well before the nominal
            # open+timeframe stamp would suggest. Intraday bars are closed at
            # their nominal close: the feed's bar is final then, and keeping
            # extended-hours bars in the frame is what the backtest dataset
            # contains (the downloader stores the same feed unfiltered). A
            # bar with no session overlap still produces no signal — only
            # actionability is session-gated, never the indicator input.
            closed_positions: list[int] = []
            for position in range(len(prepared)):
                bar_open = pd.Timestamp(prepared.iloc[position]["timestamp"])
                if bar_seconds < 86_400:
                    closed = (
                        bar_open + pd.Timedelta(seconds=bar_seconds) <= moment_ts
                    )
                else:
                    effective = _effective_bar_close(
                        calendar, bar_open, bar_seconds
                    )
                    closed = effective is not None and effective[0] <= moment_ts
                if closed:
                    closed_positions.append(position)
            if not closed_positions:
                return
            eval_frame = prepared.loc[closed_positions].reset_index(drop=True)
        else:
            closes = prepared["timestamp"] + pd.Timedelta(seconds=bar_seconds)
            closed_mask = closes <= moment_ts
            if not bool(closed_mask.any()):
                return
            # The strategy only ever sees closed bars — the same input the
            # engine gets. A still-forming bar is dropped before evaluation
            # so it cannot leak into indicators or signal rows.
            eval_frame = prepared.loc[
                closed_mask[closed_mask].index
            ].reset_index(drop=True)
        latest_open = pd.Timestamp(eval_frame.iloc[-1]["timestamp"])

        if last_processed is not None and latest_open <= last_processed:
            return
        if last_processed is None:
            # First evaluation (or restart): only the newest closed bar is
            # actionable. Older bars are stale by definition — never backfill.
            candidate_positions = [len(eval_frame) - 1]
        else:
            candidate_positions = [
                position
                for position in range(len(eval_frame))
                if eval_frame.iloc[position]["timestamp"] > last_processed
            ]
            if not candidate_positions:
                return

        # Run the strategy once over the closed-bar window — the same call
        # shape the engine makes — then read the candidate rows. The call,
        # normalization and signals-frame read run on a worker thread: over up
        # to 1000 bars per symbol they would otherwise block the daemon's
        # event loop (listeners, stream handlers, kill-switch paths) for the
        # whole evaluation. Bundles are per-deployment and evaluations are
        # sequential, so the shared engine state stays single-threaded.
        def _run_strategy_eval() -> tuple[Any, Any, pd.DataFrame | None]:
            output = bundle.engine._call_strategy(bundle.generate_signals, eval_frame)
            norm_entries, norm_exits, _indicators, _drawings = (
                bundle.engine._normalize_strategy_output(output, eval_frame)
            )
            return norm_entries, norm_exits, bundle.engine._signals_frame

        try:
            entries, exits, signals_frame = await asyncio.to_thread(
                _run_strategy_eval
            )
        except Exception as exc:  # except-kind: money-decision
            self._report_once(
                state_key[0],
                f"strategy_eval:{state_key[1]}",
                "Internal strategy evaluation failed; skipping symbol",
                deployment_id=state_key[0],
                symbol=symbol,
                error=str(exc),
            )
            return

        intents: list[tuple[int, TradeSignal]] = []
        # A signal is actionable only while the next bar is still forming:
        # grace for feed/tick jitter plus a small feed-lag allowance — never
        # a full extra bar, which would let a signal trade an hour late.
        feed_lag = min(
            SUBMIT_FEED_LAG_CAP_SECONDS,
            SUBMIT_FEED_LAG_BAR_FRACTION * bar_seconds,
        )
        max_age = pd.Timedelta(seconds=SUBMIT_GRACE_SECONDS + feed_lag)
        for position in candidate_positions:
            bar_open_ts = pd.Timestamp(eval_frame.iloc[position]["timestamp"])
            if session_bound:
                effective = _effective_bar_close(calendar, bar_open_ts, bar_seconds)
                if effective is None:
                    continue
                close_time, hits_session_end = effective
                if hits_session_end:
                    # The bar's data ends at the session close; the order can
                    # only fill at the next session open (engine parity), so
                    # the actionable window opens there.
                    next_open = _next_session_open(calendar, close_time)
                    deadline = (
                        next_open if next_open is not None else close_time
                    ) + max_age
                else:
                    deadline = close_time + max_age
                if moment_ts > deadline:
                    # Stale bar: never trade it. Still counts as processed
                    # so the runner does not reconsider it every tick.
                    continue
            else:
                close_time = _bar_close_time(bar_open_ts, bar_seconds)
                if moment_ts - close_time > max_age:
                    # Stale bar: never trade it. Still counts as processed so
                    # the runner does not reconsider it every tick.
                    continue
            row = eval_frame.iloc[position]
            signal_row = (
                signals_frame.iloc[position]
                if signals_frame is not None
                else pd.Series(dtype=object)
            )
            merged_row = row.combine_first(signal_row)
            try:
                close_price = float(merged_row["close"])
            except (TypeError, ValueError):
                continue
            if not pd.notna(close_price) or close_price <= 0:
                continue
            open_time = pd.Timestamp(eval_frame.iloc[position]["timestamp"])
            common_metadata: dict[str, Any] = {
                "source": "internal",
                "signal_timestamp": _iso(close_time),
                "internal_bar_open": _iso(open_time),
                "internal_bar_close": _iso(close_time),
                "internal_strategy": deployment.strategy_name,
            }
            if bool(exits.iloc[position]):
                exit_metadata = dict(common_metadata)
                exit_metadata.update(exit_intent_metadata(FULL_EXIT))
                exit_metadata["idempotency_key"] = _intent_idempotency_key(
                    deployment, symbol, timeframe, close_time, kind="exit"
                )
                intents.append(
                    (
                        position,
                        TradeSignal(
                            action="sell",
                            symbol=symbol,
                            entry_price=close_price,
                            stop_loss=None,
                            take_profit=None,
                            timeframe=timeframe,
                            metadata=exit_metadata,
                        ),
                    )
                )
            # ``max_open_per_symbol == 0`` means entries are disabled for this
            # deployment (cap or ledger unreadable): exits still flow, entries
            # do not — the fail-closed direction for new exposure.
            if max_open_per_symbol > 0 and bool(entries.iloc[position]):
                try:
                    direction = bundle.engine._resolve_direction(merged_row)
                except Exception as exc:  # except-kind: money-decision
                    logger.warning(
                        "Internal strategy entry has an unresolvable direction; skipping",
                        extra={
                            "data": {
                                "deployment_id": state_key[0],
                                "symbol": symbol,
                                "bar_open": _iso(open_time),
                                "error": str(exc),
                            }
                        },
                    )
                    continue
                try:
                    stop_loss, tp_levels = bundle.engine._extract_levels(merged_row)
                except Exception as exc:  # except-kind: money-decision
                    logger.warning(
                        "Internal strategy entry levels are unreadable; skipping",
                        extra={
                            "data": {
                                "deployment_id": state_key[0],
                                "symbol": symbol,
                                "bar_open": _iso(open_time),
                                "error": str(exc),
                            }
                        },
                    )
                    continue
                if _has_sizing_only_stop_fields(merged_row):
                    # Engine sizing hints cannot become a live stop and the
                    # live path cannot reproduce the engine's sizing math.
                    self._report_once(
                        state_key[0],
                        f"sizing_stop:{state_key[1]}",
                        "Internal strategy entry carries sizing-only stop fields "
                        "(sizing_sl_price/sizing_stop/sizing_sl/stop_distance); "
                        "the live path cannot honor them and the signal is skipped",
                        deployment_id=state_key[0],
                        symbol=symbol,
                    )
                    continue
                if len(tp_levels) > 1 or (
                    tp_levels and float(tp_levels[0].get("fraction", 1.0)) < 1.0
                ):
                    # The live bracket carries a single full-size take profit.
                    # Emitting only the first level would silently drop the
                    # native engine's partial-exit ladder, so this entry fails
                    # closed.
                    self._report_once(
                        state_key[0],
                        f"multi_tp:{state_key[1]}",
                        "Internal strategy entry has multiple or partial "
                        "take-profit levels; the live path supports one full-size "
                        "level and the signal is skipped",
                        deployment_id=state_key[0],
                        symbol=symbol,
                        tp_levels=[level.get("name") for level in tp_levels],
                    )
                    continue
                fallback_stop = False
                if stop_loss is None:
                    # Maintainer decision (a): a strategy without a stop gets
                    # a broker stop at the engine's fallback distance —
                    # fallback_stop_pct of the entry price, the same fallback
                    # the engine uses for sizing. Live never runs unprotected.
                    direction_sign = 1.0 if direction == "LONG" else -1.0
                    fallback_pct = float(
                        bundle.engine._risk_engine.config.fallback_stop_pct
                    )
                    stop_loss = close_price - direction_sign * close_price * fallback_pct
                    fallback_stop = True
                symbol_key = symbol.upper()
                open_count = int(open_entries.get(symbol_key, 0))
                if open_count >= max_open_per_symbol:
                    # The deployment already holds the configured number of
                    # open entries on this symbol; the runner does not
                    # pyramid beyond the cap. Exits on the same bar still go
                    # out.
                    self._report_once(
                        state_key[0],
                        f"already_open:{state_key[1]}",
                        "Internal strategy entry skipped: the deployment "
                        f"already holds {open_count} open entries on this "
                        f"symbol (cap {max_open_per_symbol})",
                        deployment_id=state_key[0],
                        symbol=symbol,
                        open_entries=open_count,
                        max_open_per_symbol=max_open_per_symbol,
                        level="warning",
                    )
                    continue
                total_open = sum(open_entries.values())
                if total_open >= TRADE_ENGINE_MAX_POSITIONS:
                    self._report_once(
                        state_key[0],
                        f"max_positions:{state_key[1]}",
                        "Internal strategy entry skipped: the deployment "
                        f"already holds {total_open} open entries "
                        f"(engine limit {TRADE_ENGINE_MAX_POSITIONS})",
                        deployment_id=state_key[0],
                        symbol=symbol,
                        open_entries_by_symbol=dict(open_entries),
                        level="warning",
                    )
                    continue
                take_profit = (
                    float(tp_levels[0]["price"]) if tp_levels else None
                )
                entry_metadata = dict(common_metadata)
                entry_metadata["internal_direction"] = direction
                if fallback_stop:
                    entry_metadata["internal_stop_source"] = "fallback_pct"
                if take_profit is None:
                    # No TP level: the strategy's own exits signal is its exit
                    # mechanism — the same unbounded-TP policy the promotion
                    # path stamps for Discord signals without a target.
                    entry_metadata["take_profit_policy"] = UNBOUNDED_TP_POLICY
                entry_metadata["idempotency_key"] = _intent_idempotency_key(
                    deployment, symbol, timeframe, close_time, kind=f"entry:{direction}"
                )
                intents.append(
                    (
                        position,
                        TradeSignal(
                            action="buy" if direction == "LONG" else "sell",
                            symbol=symbol,
                            entry_price=close_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            timeframe=timeframe,
                            metadata=entry_metadata,
                        ),
                    )
                )

        # Advance the cursor before submitting: a crash mid-submit must not
        # re-emit this bar's intents on restart (the persisted idempotency key
        # is the second layer for the intents that did reach the broker).
        self._last_bar[state_key] = latest_open

        for _position, signal in intents:
            try:
                outcome = await self._submit_signal(adapter, deployment, signal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # except-kind: money-decision
                self._note_submit_failure(
                    state_key,
                    symbol,
                    signal,
                    message="Internal strategy signal submission failed",
                    error=str(exc),
                )
                continue
            if outcome in _SUBMIT_OK_OUTCOMES:
                self._submit_failures.pop(state_key, None)
            else:
                # A refusal is not an exception, but it is still a signal that
                # never reached the broker — it counts toward the same
                # escalation so a permanently refused deployment surfaces.
                self._note_submit_failure(
                    state_key,
                    symbol,
                    signal,
                    message="Internal strategy signal was not submitted",
                    level="warning",
                    outcome=str(outcome),
                )
            if (
                (signal.metadata or {}).get("internal_direction")
                and outcome in _SUBMIT_MAY_HAVE_REACHED_BROKER
            ):
                # Count the submitted entry against this pass's caps so a
                # second entry on the same symbol later in the same pass
                # cannot slip past the per-symbol or engine limits. The
                # persisted ledger recounts it on the next pass.
                open_entries[symbol.upper()] = (
                    open_entries.get(symbol.upper(), 0) + 1
                )

    async def run_once(
        self,
        deployments: Iterable[LiveDeploymentConfig],
        adapters: Mapping[str, BaseLiveAdapter],
        *,
        now: datetime | None = None,
    ) -> None:
        """One evaluation pass over every eligible ACTIVE internal deployment."""
        for deployment in deployments:
            if not _deployment_is_active_internal(deployment):
                continue
            if not _internal_runner_opted_in(deployment):
                # Maintainer decision (e): existing ACTIVE internal
                # deployments stay inert until an operator opts in per
                # deployment. One report per deployment, then silence.
                self._report_once(
                    str(deployment.id),
                    "opt_in",
                    "Internal deployment is ACTIVE but has not opted into the "
                    "internal strategy runner; set settings."
                    f"{INTERNAL_RUNNER_ENABLED_KEY}=true to enable it",
                    deployment_id=str(deployment.id),
                    level="warning",
                )
                continue
            adapter = adapters.get(str(deployment.account_id))
            if adapter is None:
                continue
            deployment_id = str(deployment.id)
            try:
                if STRATEGY_PARAMETERS_KEY in (deployment.settings or {}):
                    # Nothing applies this override on the internal path; the
                    # strategy always runs its STRATEGY_META defaults.
                    raise ValueError(
                        f"settings.{STRATEGY_PARAMETERS_KEY} is not applied by "
                        "the internal strategy runner; promote the parameters "
                        "by writing them into STRATEGY_META"
                    )
                if deployment_signal_processor_name(deployment):
                    raise ValueError(
                        "signal_processor configuration is not applied by the "
                        "internal strategy runner; remove it or use a Discord "
                        "signal-source deployment"
                    )
                timeframe = normalize_timeframe(deployment.timeframe)
                if timeframe not in VALID_TIMEFRAMES:
                    raise ValueError(f"unsupported timeframe {deployment.timeframe!r}")
                bar_seconds = _timeframe_seconds(timeframe)
                if not bar_seconds:
                    raise ValueError(f"timeframe {timeframe!r} has no bar length")
                symbols = _deployment_symbols(deployment)
            except Exception as exc:  # except-kind: money-decision
                self._report_once(
                    deployment_id,
                    "deployment_config",
                    "Internal deployment configuration is unusable; skipping",
                    deployment_id=str(deployment.id),
                    error=str(exc),
                )
                continue
            if not _adapter_serves_bars(adapter, timeframe):
                self._report_once(
                    deployment_id,
                    f"adapter_bars:{timeframe}",
                    "Adapter cannot serve market data for this timeframe; "
                    "internal strategy evaluation fails closed",
                    deployment_id=deployment_id,
                    account_id=str(deployment.account_id),
                    adapter=type(adapter).__name__,
                    timeframe=timeframe,
                )
                continue
            if not symbols:
                continue
            bundle = self._bundle(deployment, timeframe)
            if bundle is None:
                continue
            # Entry caps need the configured per-symbol limit AND a readable
            # ledger. When either is unusable the deployment's *entries* are
            # disabled — but exits must keep flowing: skipping the whole
            # deployment would leave open positions bounded only by the
            # broker stop. ``max_open_per_symbol = 0`` is the
            # entries-disabled marker ``_evaluate_symbol`` checks.
            try:
                max_open_per_symbol = _max_open_per_symbol(deployment, bundle)
            except Exception as exc:  # except-kind: money-decision
                self._report_once(
                    deployment_id,
                    "open_position_cap",
                    "Internal deployment open-position cap is unusable; "
                    "entries are disabled and exits still evaluated",
                    deployment_id=deployment_id,
                    error=str(exc),
                )
                max_open_per_symbol = 0
            open_entries: dict[str, int] = {}
            if max_open_per_symbol > 0:
                try:
                    unfilled_refs = await asyncio.to_thread(
                        list_live_deployment_unfilled_entry_order_refs, deployment_id
                    )
                except Exception as exc:  # except-kind: money-decision
                    # Same fail-closed shape as the counts read below: an
                    # unguarded raise here would leave run_once and skip exit
                    # evaluation for this deployment AND every deployment
                    # after it in the snapshot.
                    self._report_once(
                        deployment_id,
                        "ledger",
                        "Internal deployment unfilled-entry ledger is "
                        "unreadable; entry caps cannot be enforced so entries "
                        "are disabled (exits still evaluated)",
                        deployment_id=deployment_id,
                        error=str(exc),
                    )
                    max_open_per_symbol = 0
                    unfilled_refs = set()
                working_ids: set[str] = set()
                held_symbols: set[str] | None = None
                if max_open_per_symbol > 0 and unfilled_refs:
                    # An entry with no fills counts as open only while the
                    # broker still lists its order — a dead unfilled order
                    # must not lock the symbol forever. The broker read
                    # happens only when such rows exist.
                    fetched = await self._working_order_ids(adapter, deployment)
                    if fetched is None:
                        max_open_per_symbol = 0
                    else:
                        working_ids = fetched
                        if unfilled_refs - working_ids:
                            # Some unfilled entry is no longer working: it is
                            # either dead or filled-but-unbooked, and only
                            # the account's positions can tell them apart.
                            held = await self._broker_held_symbols(
                                adapter, deployment
                            )
                            if held is None:
                                max_open_per_symbol = 0
                            else:
                                held_symbols = held
                if max_open_per_symbol > 0:
                    try:
                        open_entries = await asyncio.to_thread(
                            list_live_deployment_open_entry_counts_by_symbol,
                            deployment_id,
                            working_ids,
                            held_symbols,
                        )
                    except Exception as exc:  # except-kind: money-decision
                        # Without the ledger the entry caps cannot be
                        # enforced; entries are disabled rather than traded
                        # uncapped, and exits still flow.
                        self._report_once(
                            deployment_id,
                            "ledger",
                            "Internal deployment ledger is unreadable; entry "
                            "caps cannot be enforced so entries are disabled "
                            "(exits still evaluated)",
                            deployment_id=deployment_id,
                            error=str(exc),
                        )
                        max_open_per_symbol = 0
            for symbol in symbols:
                # Fresh wall-clock per symbol: a pass over many symbols must
                # not judge the last ones by the pass's start time.
                symbol_moment = now if now is not None else datetime.now(timezone.utc)
                try:
                    await self._evaluate_symbol(
                        adapter=adapter,
                        deployment=deployment,
                        bundle=bundle,
                        symbol=symbol,
                        timeframe=timeframe,
                        bar_seconds=bar_seconds,
                        moment=symbol_moment,
                        open_entries=open_entries,
                        max_open_per_symbol=max_open_per_symbol,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # except-kind: money-decision
                    logger.error(
                        "Internal strategy evaluation failed for symbol",
                        extra={
                            "data": {
                                "deployment_id": str(deployment.id),
                                "symbol": symbol,
                                "error": str(exc),
                            }
                        },
                    )


async def run_internal_strategy_loop(
    *,
    current_deployments: Callable[[], Iterable[LiveDeploymentConfig]],
    eligible_adapters: Callable[[], Mapping[str, BaseLiveAdapter]],
    submit_signal: SubmitSignal,
    interval_seconds: float = INTERNAL_RUNNER_TICK_SECONDS,
    runner: InternalStrategyRunner | None = None,
) -> None:
    """Drive ``runner`` forever. One failed tick never ends the loop."""
    runner = runner or InternalStrategyRunner(submit_signal=submit_signal)
    while True:
        try:
            await runner.run_once(
                current_deployments(), eligible_adapters()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # except-kind: loop-survival
            logger.error(
                "Internal strategy runner tick failed",
                extra={"data": {"error": str(exc)}},
            )
        await asyncio.sleep(interval_seconds)
