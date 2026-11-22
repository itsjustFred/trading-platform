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

A signal is only actionable while bar T+1 is still forming
(``close_age <= timeframe + SUBMIT_GRACE_SECONDS``). Older bars are stale:
their entries are skipped rather than traded late. Exits follow the same
freshness rule — a stale exit is skipped because the broker-side SL/TP legs
placed at entry remain the bound on the position.

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

from live_runner.base_adapter import BaseLiveAdapter
from live_runner.exit_signals import FULL_EXIT, exit_intent_metadata
from live_runner.promotion import strategy_target
from live_runner.signal_sources.execution_timing import _timeframe_seconds
from risk.models import TradeSignal
from runner.engine_pkg.trading_engine import TradeEngine
from runner.engines.base import BacktestConfig, resolve_strategy_parameters
from shared.constants import normalize_symbols, normalize_timeframe
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
#: Grace past the next bar's open during which a just-closed signal may still
#: be submitted. Covers feed publication delay and tick scheduling jitter; the
#: entry-deviation gate (#125) still bounds how far the fill may drift.
SUBMIT_GRACE_SECONDS = 30.0

SubmitSignal = Callable[
    [BaseLiveAdapter, LiveDeploymentConfig, TradeSignal], Awaitable[None]
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
        # Throttled one-shot logs: (key, category) already reported.
        self._reported: set[tuple[str, str]] = set()

    # ── strategy resolution ──────────────────────────────────────────────

    def _report_once(self, key: str, category: str, message: str, **extra: Any) -> None:
        marker = (key, category)
        if marker in self._reported:
            return
        self._reported.add(marker)
        logger.error(message, extra={"data": extra})

    def _bundle(self, deployment: LiveDeploymentConfig, timeframe: str) -> _StrategyBundle | None:
        """Resolve + load the deployment's strategy, cached by file mtime."""
        key = str(deployment.id)
        try:
            target = self._load_target(deployment.strategy_name)
        except Exception as exc:
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
        if cached is not None and cached.mtime_ns == mtime_ns:
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
            engine = TradeEngine(
                config,
                symbol=str(deployment.symbols[0] if deployment.symbols else ""),
            )
        except Exception as exc:
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
            mtime_ns=mtime_ns, engine=engine, generate_signals=generate_signals
        )
        self._bundles[key] = bundle
        self._reported.discard((key, "strategy_target"))
        return bundle

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
    ) -> None:
        state_key = (str(deployment.id), symbol)
        try:
            frame = await adapter.get_market_data(
                symbol, timeframe=timeframe, limit=self._bar_limit
            )
        except Exception as exc:
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
        except Exception as exc:
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

        closes = prepared["timestamp"] + pd.Timedelta(seconds=bar_seconds)
        closed_mask = closes <= pd.Timestamp(moment)
        if not bool(closed_mask.any()):
            return
        # The strategy only ever sees closed bars — the same input the engine
        # gets. A still-forming bar is dropped before evaluation so it cannot
        # leak into indicators or signal rows.
        eval_frame = prepared.loc[closed_mask[closed_mask].index].reset_index(drop=True)
        latest_open = pd.Timestamp(eval_frame.iloc[-1]["timestamp"])

        last_processed = self._last_bar.get(state_key)
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
        # shape the engine makes — then read the candidate rows.
        try:
            output = bundle.engine._call_strategy(bundle.generate_signals, eval_frame)
            entries, exits, _indicators, _drawings = (
                bundle.engine._normalize_strategy_output(output, eval_frame)
            )
        except Exception as exc:
            self._report_once(
                state_key[0],
                f"strategy_eval:{state_key[1]}",
                "Internal strategy evaluation failed; skipping symbol",
                deployment_id=state_key[0],
                symbol=symbol,
                error=str(exc),
            )
            return

        signals_frame = bundle.engine._signals_frame
        intents: list[tuple[int, TradeSignal]] = []
        max_age_seconds = bar_seconds + SUBMIT_GRACE_SECONDS
        for position in candidate_positions:
            close_time = _bar_close_time(
                pd.Timestamp(eval_frame.iloc[position]["timestamp"]), bar_seconds
            )
            age_seconds = (moment - close_time.to_pydatetime()).total_seconds()
            if age_seconds > max_age_seconds:
                # Stale bar: never trade it. Still counts as processed so the
                # runner does not reconsider it every tick.
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
            if bool(entries.iloc[position]):
                try:
                    direction = bundle.engine._resolve_direction(merged_row)
                except Exception as exc:
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
                except Exception as exc:
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
                if len(tp_levels) > 1:
                    # The live bracket carries a single take profit. Emitting
                    # only the first level would silently drop the native
                    # engine's partial-exit ladder, so this entry fails closed.
                    self._report_once(
                        state_key[0],
                        f"multi_tp:{state_key[1]}",
                        "Internal strategy entry has multiple take-profit levels; "
                        "the live path supports one and the signal is skipped",
                        deployment_id=state_key[0],
                        symbol=symbol,
                        tp_levels=[level.get("name") for level in tp_levels],
                    )
                    continue
                take_profit = (
                    float(tp_levels[0]["price"]) if tp_levels else None
                )
                entry_metadata = dict(common_metadata)
                entry_metadata["internal_direction"] = direction
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
                await self._submit_signal(adapter, deployment, signal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Internal strategy signal submission failed",
                    extra={
                        "data": {
                            "deployment_id": state_key[0],
                            "symbol": symbol,
                            "action": signal.action,
                            "error": str(exc),
                        }
                    },
                )

    async def run_once(
        self,
        deployments: Iterable[LiveDeploymentConfig],
        adapters: Mapping[str, BaseLiveAdapter],
        *,
        now: datetime | None = None,
    ) -> None:
        """One evaluation pass over every eligible ACTIVE internal deployment."""
        moment = now or datetime.now(timezone.utc)
        for deployment in deployments:
            if not _deployment_is_active_internal(deployment):
                continue
            adapter = adapters.get(str(deployment.account_id))
            if adapter is None:
                continue
            try:
                timeframe = normalize_timeframe(deployment.timeframe)
                if timeframe not in VALID_TIMEFRAMES:
                    raise ValueError(f"unsupported timeframe {deployment.timeframe!r}")
                bar_seconds = _timeframe_seconds(timeframe)
                if not bar_seconds:
                    raise ValueError(f"timeframe {timeframe!r} has no bar length")
                symbols = _deployment_symbols(deployment)
            except Exception as exc:
                self._report_once(
                    str(deployment.id),
                    "deployment_config",
                    "Internal deployment configuration is unusable; skipping",
                    deployment_id=str(deployment.id),
                    error=str(exc),
                )
                continue
            if not _adapter_serves_bars(adapter, timeframe):
                self._report_once(
                    str(deployment.id),
                    f"adapter_bars:{timeframe}",
                    "Adapter cannot serve market data for this timeframe; "
                    "internal strategy evaluation fails closed",
                    deployment_id=str(deployment.id),
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
            for symbol in symbols:
                try:
                    await self._evaluate_symbol(
                        adapter=adapter,
                        deployment=deployment,
                        bundle=bundle,
                        symbol=symbol,
                        timeframe=timeframe,
                        bar_seconds=bar_seconds,
                        moment=moment,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
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
        except Exception as exc:
            logger.error(
                "Internal strategy runner tick failed",
                extra={"data": {"error": str(exc)}},
            )
        await asyncio.sleep(interval_seconds)
