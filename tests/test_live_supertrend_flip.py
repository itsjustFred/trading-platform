"""Issue #168: the live ``supertrend_flip`` workflow on cTrader.

The manager is driven against a fake adapter that mimics the broker contract
the cTrader adapter implements (positions, protective stops, quotes, market
data, position-addressed closes, in-place stop amendment). Signals, fills and
the persisted workflow state go through the real database layer, because the
entry signal's metadata *is* the restart-safe half of the workflow state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pandas as pd
import pytest

import live_runner.supertrend_flip as flip_module
from live_runner.base_adapter import (
    ORDER_STATE_FILLED,
    ORDER_STATE_TERMINAL,
    ORDER_STATE_WORKING,
    BaseLiveAdapter,
    LiveQuote,
    ProtectiveStop,
)
from live_runner.supertrend_flip import (
    FLIP_STATE_METADATA_KEY,
    PHASE_AWAIT_GREEN,
    PHASE_BEFORE_TP1,
    PHASE_DONE,
    PHASE_EXIT_PENDING,
    PHASE_HALTED,
    PHASE_TP1_PENDING,
    LiveSupertrendFlipManager,
)
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
ACCOUNT = "acc-ctrader"
SYMBOL = "EURUSD"

TP1_PRICE = 105.0
ENTRY_PRICE = 100.0
STOP_PRICE = 95.0
POSITION_QTY = 10.0
TP1_FRACTION = 0.5
TRANCHE = 5.0


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _bars(specs: list[tuple[float, float, float]]) -> pd.DataFrame:
    """``(minute_offset, open, close)`` -> OHLCV frame with UTC timestamps."""
    rows = []
    for minute, open_price, close in specs:
        stamp = T0 + timedelta(minutes=minute)
        rows.append(
            {
                "timestamp": pd.Timestamp(stamp),
                "open": open_price,
                "high": max(open_price, close),
                "low": min(open_price, close),
                "close": close,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows)


class _FakeFlipAdapter(BaseLiveAdapter):
    """The broker contract the flip workflow needs, in memory."""

    broker_name = "ctrader"
    demo = True

    def __init__(self) -> None:
        self.positions: list[dict] = []
        self.stop_price = STOP_PRICE
        self.bid = 100.0
        self.frame = _bars([])
        self.orders: list[tuple[LiveSignal, float]] = []
        self._order_ids: dict[str, str] = {}
        self.order_states: dict[str, str] = {}
        self.auto_fill = True
        self.amends: list[float] = []
        self.submit_error: Exception | None = None
        self.quote_error: Exception | None = None
        # position_id -> persisted entry signal id the broker can prove
        # opened it. Empty means "cannot prove" (fail closed).
        self.opening_signal_ids: dict[int, str] = {}

    # ── BaseLiveAdapter abstracts ────────────────────────────────────────

    async def connect(self) -> None:
        return None

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return (100_000.0, 100_000.0)

    async def get_equity(self) -> float:
        return 100_000.0

    async def get_used_margin(self) -> float:
        return 0.0

    async def get_pip_value(self, symbol: str):
        from live_runner.base_adapter import LivePipValue

        return LivePipValue(
            value=1.0,
            units_per_qty=1.0,
            conversion_rate=1.0,
            quote_asset="USD",
            account_asset="USD",
            source="test",
        )

    @property
    def supports_stop_amendment(self) -> bool:
        return True

    @property
    def supports_supertrend_flip(self) -> bool:
        return True

    # ── broker surface the manager uses ──────────────────────────────────

    async def get_positions(self) -> list[dict]:
        return [dict(row) for row in self.positions]

    async def list_protective_stops(self) -> list[ProtectiveStop]:
        return [
            ProtectiveStop(
                order_id=str(row.get("position_id") or "pos-1"),
                symbol=str(row["symbol"]).upper(),
                side="sell",
                stop_price=self.stop_price,
            )
            for row in self.positions
            if float(row.get("qty") or 0.0) > 0
        ]

    def round_stop_price(self, stop: ProtectiveStop, price: float) -> float:
        return round(float(price), 5)

    async def amend_protective_stop(
        self, stop: ProtectiveStop, new_stop_price: float
    ) -> ProtectiveStop:
        self.amends.append(float(new_stop_price))
        self.stop_price = float(new_stop_price)
        return ProtectiveStop(
            order_id=stop.order_id,
            symbol=stop.symbol,
            side=stop.side,
            stop_price=float(new_stop_price),
        )

    async def get_quote(self, symbol: str) -> LiveQuote:
        if self.quote_error is not None:
            raise self.quote_error
        return LiveQuote(
            bid=self.bid,
            ask=self.bid + 0.0001,
            timestamp=datetime.now(timezone.utc),
            source="test",
            timestamp_source="broker",
        )

    def supports_market_data_timeframe(self, timeframe: str) -> bool:
        return True

    async def get_market_data(self, symbol, *, timeframe, limit=200):
        return self.frame

    async def get_order_state(self, order_id: str, *, client_order_id: str = ""):
        return self.order_states.get(str(order_id))

    def position_qty_tolerance(self, symbol: str) -> float:
        return 1e-9

    async def opening_signal_id_for_position(self, position_id: int):
        return self.opening_signal_ids.get(int(position_id))

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        if self.submit_error is not None:
            raise self.submit_error
        key = str(signal.id)
        if key in self._order_ids:
            # The adapter's persisted-order recovery: same signal id answers
            # with the existing broker order id, never a second order.
            return self._order_ids[key]
        order_id = f"ord-{len(self._order_ids) + 1}"
        self._order_ids[key] = order_id
        self.orders.append((signal, float(qty or 0.0)))
        if self.auto_fill:
            self._fill(signal, float(qty or 0.0))
        return order_id

    def _fill(self, signal: LiveSignal, qty: float) -> None:
        for row in self.positions:
            if str(row.get("symbol") or "").upper() != signal.symbol.upper():
                continue
            if qty >= abs(float(row.get("qty") or 0.0)):
                row["qty"] = 0.0
            else:
                row["qty"] = float(row["qty"]) - qty
            return


class _Live:
    """One cTrader account with a long EURUSD flip position."""

    def __init__(
        self,
        database,
        *,
        status: str = "ACTIVE",
        qty: float = POSITION_QTY,
        opt_in: bool = True,
    ) -> None:
        self.database = database
        database.ensure_live_account(ACCOUNT, "ctrader")
        settings = {"live_supertrend_flip": True} if opt_in else {}
        self.deployment = LiveDeploymentConfig(
            id=uuid4(),
            strategy_name="flip-probe",
            account_id=ACCOUNT,
            symbols=[SYMBOL],
            timeframe="1m",
            status=status,
            signal_source="internal",
            settings=settings,
        )
        database.create_live_deployment(self.deployment)
        self.adapter = _FakeFlipAdapter()
        self.adapter.positions = [
            {
                "symbol": SYMBOL,
                "qty": qty,
                "entry_price": ENTRY_PRICE,
                "position_id": 4242,
            }
        ]
        self.entry = self.add_signal(qty)
        # The broker proves position 4242 was opened by this entry's order.
        self.adapter.opening_signal_ids[4242] = str(self.entry.id)

    def add_signal(self, qty: float = POSITION_QTY) -> LiveSignal:
        metadata = {
            "stop_loss": STOP_PRICE,
            "take_profit": TP1_PRICE,
            "tp1_price": TP1_PRICE,
            "tp1_exit_percent": TP1_FRACTION,
            "trailing_sl_config": {
                "mode": "supertrend_flip",
                "breakeven_after_tp1": True,
                "activation_phase": "wait_for_first_green_after_tp1",
                "exit_phase": "exit_on_first_red_after_activation",
                "supertrend_period": 2,
                "supertrend_multiplier": 2.0,
            },
        }
        signal = LiveSignal(
            deployment_id=str(self.deployment.id),
            symbol=SYMBOL,
            action="buy",
            theoretical_price=ENTRY_PRICE,
            timestamp=T0,
            metadata=metadata,
        )
        self.database.save_live_signal(signal)
        self.database.save_live_execution(
            LiveExecution(
                signal_id=signal.id,
                actual_price=ENTRY_PRICE,
                filled_qty=qty,
                commission=0.0,
                timestamp=T0,
                broker_order_id=f"entry-{signal.id}",
            )
        )
        return signal

    def state(self) -> dict:
        metadata = self.database.get_live_signal_metadata(str(self.entry.id)) or {}
        return dict(metadata.get(FLIP_STATE_METADATA_KEY) or {})

    def tick(
        self,
        manager: LiveSupertrendFlipManager,
        now: datetime,
        *,
        adapter=None,
        deployments=None,
    ) -> None:
        asyncio.run(
            manager.run_once(
                {ACCOUNT: adapter or self.adapter},
                deployments if deployments is not None else [self.deployment],
                now=now,
            )
        )


def _green_red_supertrend(df, period, multiplier):
    """Deterministic Supertrend stand-in: up bars green, down bars red."""
    return pd.DataFrame(
        {
            "supertrend": (df["close"] > df["open"]).tolist(),
            "supertrend_ready": [True] * len(df),
        },
        index=df.index,
    )


@pytest.fixture
def live(isolated_db, monkeypatch) -> _Live:
    database, _engine, _data_dir = isolated_db
    monkeypatch.setattr(flip_module, "calculate_supertrend", _green_red_supertrend)
    return _Live(database)


@pytest.fixture
def surfaced(monkeypatch) -> list[tuple[str, dict]]:
    """Every failure the flip loop surfaces: (log message, Sentry context)."""
    events: list[tuple[str, dict]] = []
    logged: list[str] = []

    def _capture(exc, *, service_name=None, context=None):
        events.append((logged[-1] if logged else "", {**(context or {}), "error": repr(exc)}))

    def _error(message, *args, **kwargs):
        logged.append(str(message))

    monkeypatch.setattr(flip_module, "capture_exception", _capture)
    monkeypatch.setattr(flip_module.logger, "error", _error)
    monkeypatch.setattr(flip_module.logger, "critical", _error)
    return events


# ── TP1 ──────────────────────────────────────────────────────────────────────


def test_tp1_partial_close_fires_on_quote_and_confirms_by_quantity(
    live: _Live, surfaced
) -> None:
    manager = LiveSupertrendFlipManager()

    live.adapter.bid = 104.0
    live.tick(manager, _at(0.1))
    assert live.adapter.orders == []
    assert live.state().get("phase", PHASE_BEFORE_TP1) == PHASE_BEFORE_TP1

    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))
    # The tranche is half the 10-lot position; the close is exit-only and
    # addressed at the broker position.
    assert len(live.adapter.orders) == 1
    signal, qty = live.adapter.orders[0]
    assert qty == TRANCHE
    assert signal.metadata["exit_only"] is True
    assert signal.metadata["risk_sizing_mode"] == "exit_only"
    assert signal.metadata["position_id"] == 4242
    assert signal.metadata["flip_role"] == "tp1"
    assert live.state()["phase"] == PHASE_TP1_PENDING

    # The fake fill already dropped the quantity: the next pass confirms TP1
    # and arms the workflow at the bar after the TP1 bar.
    live.tick(manager, _at(0.3))
    state = live.state()
    assert state["phase"] == PHASE_AWAIT_GREEN
    assert state["tp1_qty"] == TRANCHE
    assert state["tp1_bar_open"] is not None
    assert state["last_bar_time"] == state["tp1_bar_open"]
    assert surfaced == []


def test_tp1_waits_for_broker_confirmation_before_arming(live: _Live) -> None:
    manager = LiveSupertrendFlipManager()
    live.adapter.auto_fill = False
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))
    assert live.state()["phase"] == PHASE_TP1_PENDING

    # Quantity unchanged and the order still working: no second submit, no arm.
    live.adapter.order_states["ord-1"] = ORDER_STATE_WORKING
    live.tick(manager, _at(0.3))
    live.tick(manager, _at(0.4))
    assert len(live.adapter.orders) == 1
    assert live.state()["phase"] == PHASE_TP1_PENDING

    # The fill lands: quantity drops by the tranche and the workflow arms.
    live.adapter.positions[0]["qty"] = POSITION_QTY - TRANCHE
    live.tick(manager, _at(0.5))
    assert live.state()["phase"] == PHASE_AWAIT_GREEN


def test_tp1_terminal_order_retries_under_a_new_deterministic_id(
    live: _Live, surfaced
) -> None:
    manager = LiveSupertrendFlipManager()
    live.adapter.auto_fill = False
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))
    first_signal_id = str(live.adapter.orders[0][0].id)
    live.adapter.order_states["ord-1"] = ORDER_STATE_TERMINAL

    live.tick(manager, _at(0.3))
    assert len(live.adapter.orders) == 2
    second_signal_id = str(live.adapter.orders[1][0].id)
    assert second_signal_id != first_signal_id
    # Same tranche, same position: the retry cannot be a different order.
    assert live.adapter.orders[1][1] == TRANCHE
    assert live.state()["tp1_attempts"] == 1


def test_tp1_zero_tranche_is_skipped_like_replay(isolated_db, monkeypatch) -> None:
    database, _engine, _data_dir = isolated_db
    monkeypatch.setattr(flip_module, "calculate_supertrend", _green_red_supertrend)
    small = _Live(database, qty=1.0)
    manager = LiveSupertrendFlipManager()
    small.adapter.bid = 106.0

    small.tick(manager, _at(0.2))
    small.tick(manager, _at(0.3))

    assert small.adapter.orders == []
    state = small.state()
    assert state["phase"] == PHASE_BEFORE_TP1
    assert state["tp1_skipped_time"] is not None


def test_restart_recovers_tp1_state_from_persisted_metadata(live: _Live) -> None:
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))
    live.tick(manager, _at(0.3))
    assert live.state()["phase"] == PHASE_AWAIT_GREEN

    # A brand-new manager (the restarted daemon) must not repeat TP1: the
    # persisted state plus the broker quantity are the whole truth.
    restarted = LiveSupertrendFlipManager()
    live.tick(restarted, _at(0.4))
    assert len(live.adapter.orders) == 1
    assert live.state()["phase"] == PHASE_AWAIT_GREEN


# ── breakeven ────────────────────────────────────────────────────────────────


def _arm_tp1(live: _Live, manager: LiveSupertrendFlipManager) -> None:
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))
    live.tick(manager, _at(0.3))
    assert live.state()["phase"] == PHASE_AWAIT_GREEN


def test_breakeven_amends_only_after_the_tp1_bar_closes(live: _Live) -> None:
    manager = LiveSupertrendFlipManager()
    _arm_tp1(live, manager)

    # TP1 triggered at 15:00:20 -> TP1 bar opens 15:00, closes 15:01.
    live.tick(manager, _at(0.5))  # 15:00:30 - TP1 bar still open
    assert live.adapter.amends == []

    live.tick(manager, _at(1.05))  # 15:01:03 - TP1 bar closed
    assert live.adapter.amends == [ENTRY_PRICE]
    assert live.adapter.stop_price == ENTRY_PRICE
    assert live.state()["breakeven_done"] is True

    # Idempotent: the broker already shows breakeven, no second amend.
    live.tick(manager, _at(1.2))
    assert live.adapter.amends == [ENTRY_PRICE]


def test_breakeven_survives_restart(live: _Live) -> None:
    manager = LiveSupertrendFlipManager()
    _arm_tp1(live, manager)
    # No pass past the TP1-bar close yet: the breakeven amend is still owed.
    # A restarted manager picks it up from the persisted state and the broker
    # stop - it does not need the dead process's memory.
    restarted = LiveSupertrendFlipManager()
    live.tick(restarted, _at(1.05))
    assert live.adapter.amends == [ENTRY_PRICE]
    assert live.state()["breakeven_done"] is True


# ── Supertrend bars and the flip exit ────────────────────────────────────────


def _feed_bars(live: _Live, specs: list[tuple[float, float, float]]) -> None:
    live.adapter.frame = _bars(specs)


def test_flip_exit_waits_for_green_then_exits_on_red(live: _Live, surfaced) -> None:
    manager = LiveSupertrendFlipManager()
    _arm_tp1(live, manager)
    live.tick(manager, _at(1.05))  # breakeven lands

    # A red bar while awaiting green does NOT exit (replay parity).
    _feed_bars(live, [(1.0, 100.0, 99.0), (2.0, 99.0, 98.0)])
    live.tick(manager, _at(3.1))
    state = live.state()
    assert state["phase"] == PHASE_AWAIT_GREEN
    assert len(live.adapter.orders) == 1  # only the TP1 close so far

    # Green activates, the next red flips and the remainder closes.
    _feed_bars(
        live,
        [
            (1.0, 100.0, 99.0),
            (2.0, 99.0, 98.0),
            (3.0, 98.0, 101.0),
            (4.0, 101.0, 97.0),
        ],
    )
    live.tick(manager, _at(5.1))
    state = live.state()
    assert state["phase"] == PHASE_EXIT_PENDING
    assert state["activation_time"] is not None
    assert state["flip_bar_time"] is not None
    exits = [o for o in live.adapter.orders if o[0].metadata.get("flip_role") == "flip-exit"]
    assert len(exits) == 1
    assert exits[0][1] == POSITION_QTY - TRANCHE  # the remainder is the other half


def test_bars_at_or_before_the_tp1_bar_are_dead(live: _Live) -> None:
    manager = LiveSupertrendFlipManager()
    _arm_tp1(live, manager)

    # The TP1 bar itself (open 15:00) and earlier bars must never activate or
    # flip the workflow, even when they are green/red.
    _feed_bars(
        live,
        [
            (0.0, 90.0, 130.0),  # huge green bar at/before TP1 bar open
            (1.0, 100.0, 101.0),  # first live bar: green -> activate
            (2.0, 101.0, 99.0),  # red -> flip
        ],
    )
    live.tick(manager, _at(3.1))
    state = live.state()
    assert state["phase"] == PHASE_EXIT_PENDING
    # activation happened on the 15:01 bar, not the dead 15:00 one.
    assert state["activation_time"].startswith("2026-01-05T15:01")


def test_exit_pending_resubmits_idempotently_across_restart(live: _Live) -> None:
    manager = LiveSupertrendFlipManager()
    _arm_tp1(live, manager)
    _feed_bars(live, [(1.0, 100.0, 101.0), (2.0, 101.0, 99.0)])
    live.tick(manager, _at(3.1))
    assert live.state()["phase"] == PHASE_EXIT_PENDING
    assert live.adapter.positions[0]["qty"] == 0.0

    # Position still shows dust while the exit order works: a restarted
    # manager keeps the pending state and does not fire a second close.
    live.adapter.positions[0]["qty"] = 4.9
    live.adapter.auto_fill = False
    restarted = LiveSupertrendFlipManager()
    exit_orders_before = len(
        [o for o in live.adapter.orders if o[0].metadata.get("flip_role") == "flip-exit"]
    )
    live.adapter.order_states["ord-2"] = ORDER_STATE_WORKING
    live.tick(restarted, _at(3.2))
    exit_orders_after = len(
        [o for o in live.adapter.orders if o[0].metadata.get("flip_role") == "flip-exit"]
    )
    assert exit_orders_after == exit_orders_before
    assert live.state()["phase"] == PHASE_EXIT_PENDING


def test_exit_pending_filled_with_unconfirmed_ledger_waits(
    live: _Live,
) -> None:
    """A FILLED exit whose ledger has not caught up and whose position read
    is stale must WAIT (bounded by the pending timeout), not resubmit - the
    same rule TP1 follows, so a lagging ledger cannot burn close attempts
    or double-close the remainder."""
    manager = LiveSupertrendFlipManager()
    _arm_tp1(live, manager)
    _feed_bars(live, [(1.0, 100.0, 101.0), (2.0, 101.0, 99.0)])
    live.adapter.auto_fill = False  # the exit order stays working/unfilled
    live.tick(manager, _at(3.1))
    assert live.state()["phase"] == PHASE_EXIT_PENDING
    orders_before = len(live.adapter.orders)

    # Broker says the exit order FILLED; the ledger shows nothing yet and
    # the position read is stale (still shows the remainder).
    live.adapter.order_states["ord-2"] = ORDER_STATE_FILLED
    live.tick(manager, _at(3.2))
    live.tick(manager, _at(3.3))

    assert len(live.adapter.orders) == orders_before  # no resubmit
    assert live.state()["phase"] == PHASE_EXIT_PENDING


# ── fail-closed paths ────────────────────────────────────────────────────────


def test_non_flip_entries_are_untouched(live: _Live) -> None:
    live.database.merge_live_signal_metadata(
        str(live.entry.id),
        {"trailing_sl_config": {"mode": "percent", "percent": 0.01}},
    )
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0
    _feed_bars(live, [(1.0, 100.0, 101.0)])

    live.tick(manager, _at(0.2))
    live.tick(manager, _at(3.1))
    assert live.adapter.orders == []
    assert live.state() == {}


def test_short_position_is_not_managed(live: _Live) -> None:
    live.adapter.positions[0]["qty"] = -POSITION_QTY
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))
    assert live.adapter.orders == []


def test_two_position_rows_for_one_symbol_halt_fail_closed(
    live: _Live, surfaced
) -> None:
    live.adapter.positions.append(
        {
            "symbol": SYMBOL,
            "qty": 3.0,
            "entry_price": ENTRY_PRICE,
            "position_id": 7777,
        }
    )
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))
    assert live.adapter.orders == []
    assert live.state()["phase"] == PHASE_HALTED
    assert any("halted" in message for message, _ctx in surfaced)


def test_malformed_persisted_state_halts_instead_of_guessing(
    live: _Live, surfaced
) -> None:
    live.database.merge_live_signal_metadata(
        str(live.entry.id),
        {FLIP_STATE_METADATA_KEY: {"phase": "sideways"}},
    )
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))
    assert live.adapter.orders == []
    assert live.state()["phase"] == PHASE_HALTED


def test_quote_failure_retries_without_submitting(live: _Live, surfaced) -> None:
    manager = LiveSupertrendFlipManager()
    live.adapter.quote_error = RuntimeError("spot feed down")
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))
    assert live.adapter.orders == []
    assert live.state().get("phase", PHASE_BEFORE_TP1) == PHASE_BEFORE_TP1
    assert surfaced != []

    # Recovery: the next healthy pass (after the failure backoff) triggers TP1.
    live.adapter.quote_error = None
    live.tick(manager, _at(0.7))
    assert len(live.adapter.orders) == 1


def test_paused_deployment_still_manages_its_position(isolated_db, monkeypatch) -> None:
    database, _engine, _data_dir = isolated_db
    monkeypatch.setattr(flip_module, "calculate_supertrend", _green_red_supertrend)
    paused = _Live(database, status="PAUSED")
    manager = LiveSupertrendFlipManager()
    paused.adapter.bid = 106.0

    paused.tick(manager, _at(0.2))
    # Every workflow action is a close or a protective amendment of an
    # already-open position, which a pause explicitly still allows (#126).
    assert len(paused.adapter.orders) == 1


def test_alpaca_adapter_is_never_eligible(live: _Live) -> None:
    class _NoAmendAdapter(_FakeFlipAdapter):
        @property
        def supports_supertrend_flip(self) -> bool:
            return False

    adapter = _NoAmendAdapter()
    adapter.positions = [dict(row) for row in live.adapter.positions]
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2), adapter=adapter)
    assert adapter.orders == []


# ── deployment opt-in and broker gate (#168 stage 4) ─────────────────────────


def test_opt_in_is_off_by_default(isolated_db, monkeypatch) -> None:
    """A flip entry signal alone never enables the workflow: the deployment
    must carry the explicit settings opt-in."""
    database, _engine, _data_dir = isolated_db
    monkeypatch.setattr(flip_module, "calculate_supertrend", _green_red_supertrend)
    plain = _Live(database, opt_in=False)
    manager = LiveSupertrendFlipManager()
    plain.adapter.bid = 106.0
    _feed_bars(plain, [(1.0, 100.0, 101.0), (2.0, 101.0, 99.0)])

    plain.tick(manager, _at(0.2))
    plain.tick(manager, _at(3.1))
    assert plain.adapter.orders == []
    assert plain.state() == {}


def test_explicit_opt_in_values_enable_the_workflow(isolated_db, monkeypatch) -> None:
    database, _engine, _data_dir = isolated_db
    monkeypatch.setattr(flip_module, "calculate_supertrend", _green_red_supertrend)
    for value in (True, "true", "1", "yes", "on"):
        opted = _Live(database, opt_in=False)
        opted.deployment.settings = {"live_supertrend_flip": value}
        manager = LiveSupertrendFlipManager()
        opted.adapter.bid = 106.0
        opted.tick(manager, _at(0.2), deployments=[opted.deployment])
        assert len(opted.adapter.orders) == 1, f"opt-in value {value!r} did not enable TP1"


def test_unrecognized_opt_in_value_stays_off(isolated_db, monkeypatch) -> None:
    database, _engine, _data_dir = isolated_db
    monkeypatch.setattr(flip_module, "calculate_supertrend", _green_red_supertrend)
    typo = _Live(database, opt_in=False)
    typo.deployment.settings = {"live_supertrend_flip": "treu"}
    manager = LiveSupertrendFlipManager()
    typo.adapter.bid = 106.0

    typo.tick(manager, _at(0.2), deployments=[typo.deployment])
    assert typo.adapter.orders == []


def test_alpaca_broker_is_outside_the_supported_set(live: _Live) -> None:
    """Even fully opted in, a non-cTrader adapter is never managed: Alpaca's
    route waits on #350's paper run."""

    class _AlpacaLikeAdapter(_FakeFlipAdapter):
        broker_name = "alpaca"

    adapter = _AlpacaLikeAdapter()
    adapter.positions = [dict(row) for row in live.adapter.positions]
    manager = LiveSupertrendFlipManager()

    live.tick(manager, _at(0.2), adapter=adapter)
    assert adapter.orders == []


# ── #393 review blockers: intent-first persistence, broker accounting,
#    position binding, exit-lock routing, breakeven guard ────────────────────


def test_tp1_intent_is_persisted_before_submit(live: _Live) -> None:
    """B1: the TP1 phase/qty/signal-id must be durable BEFORE submit_order is
    called, so a crash between submit and persist cannot lose the intent."""
    seen: list[dict] = []
    orig_submit = live.adapter.submit_order

    async def spy_submit(signal, *, qty=None):
        seen.append(dict(live.state()))
        return await orig_submit(signal, qty=qty)

    live.adapter.submit_order = spy_submit
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.2))

    assert len(seen) == 1
    assert seen[0]["phase"] == "tp1_pending"
    assert seen[0]["tp1_qty"] == TRANCHE
    assert seen[0]["tp1_signal_id"]
    assert seen[0]["tp1_pending_since"]


def test_tp1_partial_fill_is_accepted_and_never_resubmitted(
    live: _Live, surfaced
) -> None:
    """B2: a broker partial fill completes the tranche - the remainder is
    recorded, nothing is resubmitted, the workflow advances."""
    live.adapter.submit_order_orig = live.adapter.submit_order

    async def partial_submit(signal, *, qty=None):
        # Fill only part of the requested tranche, like a volume-step floor.
        filled = min(float(qty or 0.0), TRANCHE - 1.0)
        live.adapter._order_ids[str(signal.id)] = f"ord-{len(live.adapter._order_ids)+1}"
        live.adapter.orders.append((signal, float(qty or 0.0)))
        for row in live.adapter.positions:
            row["qty"] = float(row["qty"]) - filled
        return live.adapter._order_ids[str(signal.id)]

    live.adapter.submit_order = partial_submit
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))
    live.tick(manager, _at(0.3))

    state = live.state()
    assert state["phase"] == "await_green"
    assert state.get("tp1_filled_qty") == pytest.approx(TRANCHE - 1.0)
    tp1_orders = [
        o for o in live.adapter.orders if o[0].metadata.get("flip_role") == "tp1"
    ]
    assert len(tp1_orders) == 1


def test_position_id_mismatch_takes_no_action(live: _Live, surfaced) -> None:
    """B3: a plan bound to position 4242 must not manage a different position
    id - alert + done, zero orders."""
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))  # TP1 submitted, state bound to 4242
    assert live.state()["position_id"] == 4242

    # The original position is replaced by a new one (manual close + reopen).
    live.adapter.positions = [
        {
            "symbol": SYMBOL,
            "qty": POSITION_QTY,
            "entry_price": ENTRY_PRICE,
            "position_id": 9999,
        }
    ]
    live.tick(manager, _at(0.4))

    orders_after = len(live.adapter.orders)
    live.adapter.bid = 200.0
    live.tick(manager, _at(0.5))
    assert len(live.adapter.orders) == orders_after
    assert live.state()["phase"] == "done"
    assert any("mismatch" in m for m, _c in surfaced)


def test_flip_close_uses_exit_lock_and_in_flight_check(live: _Live) -> None:
    """SF6: flip closes route through the injected exit lock and the
    in-flight exit check before reaching the adapter."""
    import asyncio as _aio

    lock_calls: list[tuple[str, str]] = []
    check_calls: list[str] = []
    real_lock = _aio.Lock()

    def exit_lock(account_id, symbol):
        lock_calls.append((account_id, symbol))
        return real_lock

    async def in_flight(adapter, deployment, symbol):
        check_calls.append(symbol)
        return []

    manager = LiveSupertrendFlipManager(
        exit_lock=exit_lock, exit_in_flight_check=in_flight
    )
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))

    assert lock_calls == [(ACCOUNT, SYMBOL)]
    assert check_calls == [SYMBOL]


def test_flip_close_waits_for_the_account_order_lock(
    live: _Live, monkeypatch
) -> None:
    """The flip submit is one section under ``account_order_placement``:
    while another section of the same account holds the lock the close is
    refused (bounded wait), never submitted on a possibly-stale read."""
    import live_runner.account_order_lock as lock_module

    # Shrink the bounded wait so the refusal is fast instead of 120s.
    monkeypatch.setattr(lock_module, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.2)
    live.adapter.bid = 106.0
    manager = LiveSupertrendFlipManager()

    async def scenario() -> None:
        # Same event loop as run_once -> the same per-loop lock the submit
        # path must acquire.
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            async with lock_module.account_order_placement(ACCOUNT):
                held.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await held.wait()
        try:
            await manager.run_once(
                {ACCOUNT: live.adapter}, [live.deployment], now=_at(0.2)
            )
        finally:
            release.set()
            await holder

    asyncio.run(scenario())
    assert live.adapter.orders == []

    live.tick(manager, _at(1.0))
    assert len(live.adapter.orders) == 1


def test_in_flight_exit_defers_flip_close(live: _Live) -> None:
    """SF6: a working exit for the deployment defers the flip close - no
    order is submitted and no failure is recorded."""
    async def busy(adapter, deployment, symbol):
        return [{"signal_id": "other-exit"}]

    manager = LiveSupertrendFlipManager(exit_in_flight_check=busy)
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))
    live.tick(manager, _at(0.3))

    assert live.adapter.orders == []
    assert live.state()["phase"] == "tp1_pending"


def test_breakeven_never_loosens_a_tighter_stop(live: _Live) -> None:
    """SF9: a stop already above entry (manually tightened) is left alone."""
    manager = LiveSupertrendFlipManager()
    # TP1 then confirm.
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))
    live.tick(manager, _at(0.3))
    assert live.state()["phase"] == "await_green"

    # A green bar activates; TP1 bar must be dead first - use bars helper
    # through the manager's own flow is complex; instead drive breakeven via
    # a post-TP1 state and a stop above entry.
    live.adapter.stop_price = ENTRY_PRICE + 3.0  # manually tightened above BE
    state = live.state()
    state["phase"] = "active"
    state["tp1_bar_open"] = _at(0.0).isoformat()
    state["breakeven_done"] = False
    live.database.merge_live_signal_metadata(
        str(live.entry.id),
        {FLIP_STATE_METADATA_KEY: state},
    )
    # A closed green bar after the TP1 bar keeps the bar step healthy so the
    # pass reaches its persist.
    live.adapter.frame = _bars([(5.0, 100.0, 101.0)])
    live.tick(manager, _at(10.0))
    # The pass must have reached the breakeven branch (it marks itself done
    # without amending), and must not have loosened the tighter stop.
    assert live.state()["breakeven_done"] is True
    assert live.adapter.amends == []

    # And it must actually be non-vacuous: with the stop at entry the same
    # pass DOES amend - proving the comparison above decided, not skipped.
    live2 = _Live(live.database)
    # A stop BELOW entry is exactly what breakeven exists to fix: the same
    # pass must amend it up to the entry price.
    live2.adapter.stop_price = ENTRY_PRICE - 3.0
    state2 = live2.state()
    state2["phase"] = "active"
    state2["tp1_bar_open"] = _at(0.0).isoformat()
    state2["breakeven_done"] = False
    live2.database.merge_live_signal_metadata(
        str(live2.entry.id),
        {FLIP_STATE_METADATA_KEY: state2},
    )
    live2.adapter.frame = _bars([(5.0, 100.0, 101.0)])
    live2.tick(LiveSupertrendFlipManager(), _at(10.0))
    assert live2.adapter.amends == [ENTRY_PRICE]


# ── First-sight provenance (#168) ────────────────────────────────────────────


def test_first_sight_rejects_a_same_size_foreign_position(
    live: _Live, surfaced
) -> None:
    """Quantity coincidence never binds: only proven opening order does."""
    # The position has exactly the ledgered size, but the broker attributes
    # its opening order to somebody else's signal.
    live.adapter.opening_signal_ids[4242] = str(uuid4())
    manager = LiveSupertrendFlipManager()

    live.tick(manager, _at(0.1))

    assert live.state().get("phase") == PHASE_DONE
    assert live.adapter.orders == []
    assert live.adapter.amends == []
    assert any("first sight" in msg for msg, _ in surfaced)


def test_first_sight_retries_a_transient_provenance_read(
    live: _Live, surfaced
) -> None:
    """An unreadable broker read is transient, never proven foreign: the
    pass fails, backs off and retries; only a PROVEN foreign opener ends
    the plan. The protective stop stays in force throughout."""
    calls = {"n": 0}

    async def _flaky(position_id: int) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("order list unreadable")
        return live.adapter.opening_signal_ids.get(int(position_id))

    live.adapter.opening_signal_id_for_position = _flaky  # type: ignore[method-assign]
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.1))
    # Nothing persisted as done; the failure was surfaced as a retry.
    assert live.state().get("phase") != PHASE_DONE
    assert live.adapter.orders == []
    assert any("retried" in msg for msg, _ in surfaced)

    live.tick(manager, _at(0.2))  # still inside the 15s first backoff
    assert calls["n"] == 1

    live.tick(manager, _at(1.0))  # retry: provenance now proves the opener
    assert calls["n"] == 2
    assert live.state().get("phase") != PHASE_DONE
    assert len(live.adapter.orders) == 1  # the TP1 close went out


def test_first_sight_provenance_gives_up_but_stays_recoverable(
    live: _Live, surfaced
) -> None:
    """Past FLIP_GIVE_UP_AFTER_SECONDS the workflow stops retrying every
    tick and probes instead - the plan is NEVER marked done on a read it
    could not complete, and a later successful read still binds it."""
    live.adapter.opening_signal_ids.clear()

    async def _raise(position_id: int) -> str | None:
        raise RuntimeError("order list unreadable")

    live.adapter.opening_signal_id_for_position = _raise  # type: ignore[method-assign]
    manager = LiveSupertrendFlipManager()

    # Drive failures past the 40-minute give-up horizon. Backoff doubles
    # 15s -> 300s cap, so probing every 5 minutes exercises both states.
    for minute in range(0, 46):
        live.tick(manager, _at(minute * 1.0 + 0.1))

    assert live.state().get("phase") != PHASE_DONE
    assert live.adapter.orders == []
    critical = [msg for msg, _ in surfaced if "given up" in msg]
    assert len(critical) == 1

    # Recovery: the broker read works again and the next allowed probe
    # (15 minutes after give-up) binds the plan instead of abandoning it.
    async def _recovered(position_id: int) -> str | None:
        return live.adapter.opening_signal_ids.get(int(position_id))

    live.adapter.opening_signal_id_for_position = _recovered  # type: ignore[method-assign]
    live.adapter.opening_signal_ids[4242] = str(live.entry.id)
    live.adapter.bid = 106.0
    live.tick(manager, _at(62.0))
    assert live.state().get("phase") != PHASE_DONE
    assert len(live.adapter.orders) == 1


def test_done_plan_alerts_once_not_every_pass(live: _Live, surfaced) -> None:
    """A finished plan facing a foreign position must not re-alert."""
    live.adapter.opening_signal_ids.clear()
    manager = LiveSupertrendFlipManager()

    live.tick(manager, _at(0.1))
    live.tick(manager, _at(0.2))
    live.tick(manager, _at(0.3))

    assert live.state().get("phase") == PHASE_DONE
    assert len(surfaced) == 1


def test_done_plan_survives_a_restart_without_realerting(
    live: _Live, surfaced
) -> None:
    """The persisted ``done`` gate is what makes the alert once-only across
    restarts: a NEW manager facing the same foreign position must take no
    action and surface nothing."""
    live.adapter.opening_signal_ids[4242] = str(uuid4())
    first = LiveSupertrendFlipManager()
    live.tick(first, _at(0.1))
    assert live.state().get("phase") == PHASE_DONE
    assert len(surfaced) == 1

    restarted = LiveSupertrendFlipManager()
    for minute in range(1, 5):
        live.tick(restarted, _at(minute * 0.25))

    assert live.state().get("phase") == PHASE_DONE
    assert live.adapter.orders == []
    assert len(surfaced) == 1


def test_done_plan_foreign_position_after_restart_alerts_once(
    live: _Live, surfaced
) -> None:
    """Alert-once across a restart, facing a DIFFERENT position.

    The ``done``/``halted`` early return in ``_prepare_position`` is what
    keeps a finished plan from re-alerting every pass; the case it exists
    for is a later position on the same symbol, not the bound one. Removing
    the early return makes the identity mismatch fire on every tick.
    """
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0
    live.tick(manager, _at(0.2))  # bound to position 4242
    live.adapter.positions = [
        {
            "symbol": SYMBOL,
            "qty": POSITION_QTY,
            "entry_price": ENTRY_PRICE,
            "position_id": 9999,
        }
    ]
    live.tick(manager, _at(0.4))  # mismatch -> done, one alert
    assert live.state()["phase"] == PHASE_DONE
    surfaced_count = len(surfaced)
    order_count = len(live.adapter.orders)

    restarted = LiveSupertrendFlipManager()
    for minute in range(1, 5):
        live.tick(restarted, _at(0.5 + minute * 0.25))

    assert live.state()["phase"] == PHASE_DONE
    assert len(surfaced) == surfaced_count
    assert len(live.adapter.orders) == order_count


def test_give_up_alert_is_critical_and_probes_add_no_surface(
    live: _Live, monkeypatch
) -> None:
    """The give-up surface is the one alert the decision calls critical:
    exactly one ``logger.critical`` past ``FLIP_GIVE_UP_AFTER_SECONDS``, and
    the given-up probes that follow add no critical surface, no order and
    no stop amendment - the broker-side stop stays untouched."""
    critical_msgs: list[str] = []
    error_msgs: list[str] = []

    def _critical(message, *args, **kwargs):
        critical_msgs.append(str(message))

    def _error(message, *args, **kwargs):
        error_msgs.append(str(message))

    monkeypatch.setattr(flip_module.logger, "critical", _critical)
    monkeypatch.setattr(flip_module.logger, "error", _error)
    monkeypatch.setattr(flip_module, "capture_exception", lambda *a, **k: None)

    live.adapter.opening_signal_ids.clear()

    async def _raise(position_id: int) -> str | None:
        raise RuntimeError("order list unreadable")

    live.adapter.opening_signal_id_for_position = _raise  # type: ignore[method-assign]
    manager = LiveSupertrendFlipManager()

    # Drive failures past the 40-minute give-up horizon (backoff doubles
    # 15s -> 300s cap), then keep probing: probes past
    # FLIP_GIVEN_UP_PROBE_SECONDS must not re-surface.
    for minute in range(0, 90):
        live.tick(manager, _at(minute * 1.0 + 0.1))

    assert live.state().get("phase") != PHASE_DONE
    assert live.adapter.orders == []
    assert live.adapter.amends == []
    give_up = [msg for msg in critical_msgs if "given up" in msg]
    assert len(give_up) == 1
    assert not any("given up" in msg for msg in error_msgs)

    # Recovery half: the read works again and the next probe binds the plan.
    async def _recovered(position_id: int) -> str | None:
        return live.adapter.opening_signal_ids.get(int(position_id))

    live.adapter.opening_signal_ids[4242] = str(live.entry.id)
    live.adapter.opening_signal_id_for_position = _recovered  # type: ignore[method-assign]
    live.adapter.bid = 106.0
    live.tick(manager, _at(120.0))
    assert live.state().get("phase") != PHASE_DONE
    assert len(live.adapter.orders) == 1


def test_unimplemented_provenance_retries_and_never_binds(
    live: _Live, surfaced
) -> None:
    """The base ``opening_signal_id_for_position`` RAISES ``NotImplementedError``:
    ``None`` would read as proven-foreign and end the plan, so an adapter
    that never implemented the read must instead take the transient-retry
    path - never bind, never mark ``done``."""
    base_read = BaseLiveAdapter.opening_signal_id_for_position.__get__(live.adapter)
    with pytest.raises(NotImplementedError):
        asyncio.run(base_read(4242))

    live.adapter.opening_signal_id_for_position = base_read  # type: ignore[method-assign]
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    for minute in (0.1, 1.0, 5.0):
        live.tick(manager, _at(minute))

    assert live.state().get("phase") != PHASE_DONE
    assert live.adapter.orders == []
    assert any("retried" in msg or "step failed" in msg for msg, _ in surfaced)


def test_first_sight_accepts_the_proven_position(live: _Live) -> None:
    """The happy path still manages: provenance proves, workflow proceeds."""
    manager = LiveSupertrendFlipManager()
    live.adapter.bid = 106.0

    live.tick(manager, _at(0.1))
    live.tick(manager, _at(0.2))

    # TP1 tranche went out - the workflow is live, not done.
    assert live.state().get("phase") != PHASE_DONE
    assert len(live.adapter.orders) == 1


def test_flip_lookback_window_converges_to_full_history_supertrend() -> None:
    """The live window ``max(300, period*30)`` must reproduce the replay's
    full-history Supertrend on recent bars: the Wilder-RMA seed dependence
    has to be negligible at the tail, or live and replay drift."""
    import numpy as np

    from live_runner.supertrend_flip import FLIP_BAR_LOOKBACK
    from signal_bots.ramjet.processor import calculate_supertrend

    rng = np.random.default_rng(7)
    n = 2400
    closes = 100.0 + np.cumsum(rng.normal(0.01, 0.15, n))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + rng.random(n) * 0.1
    lows = np.minimum(opens, closes) - rng.random(n) * 0.1
    index = pd.date_range("2026-01-05", periods=n, freq="min", tz="UTC")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes},
        index=index,
    )
    ohlc = df[["open", "high", "low", "close"]]

    for period in (2, 10, 20):
        lookback = max(FLIP_BAR_LOOKBACK, period * 30, period + 30)
        full = calculate_supertrend(ohlc, period, 2.0)
        window = calculate_supertrend(
            ohlc.tail(lookback).reset_index(drop=True), period, 2.0
        )
        tail = 60
        assert list(window["supertrend"].tail(tail)) == list(
            full["supertrend"].tail(tail)
        )
        assert list(window["supertrend_ready"].tail(tail)) == list(
            full["supertrend_ready"].tail(tail)
        )


# ── Gate matrix: who may run supertrend_flip at all (#168) ──────────────────


def _flip_gate_deployment(*, opted: bool) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=uuid4(),
        strategy_name="flip-gate",
        account_id=ACCOUNT,
        symbols=[SYMBOL],
        timeframe="1m",
        status="ACTIVE",
        signal_source="internal",
        settings={"live_supertrend_flip": True} if opted else {},
    )


def _flip_gate_adapter(**overrides):
    from types import SimpleNamespace

    # The gates read plain attributes; a namespace keeps each case honest
    # (a property on the fake would make "capability removed" untestable).
    attrs = {
        "broker_name": "ctrader",
        "supports_supertrend_flip": True,
        "supports_stop_amendment": True,
        "demo": True,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


@pytest.mark.parametrize(
    ("opted", "demo", "broker", "flip_capable", "amend", "expected"),
    [
        # Only the full combination admits the mode.
        (True, True, "ctrader", True, True, True),
        # Deployment opt-in removed -> mode refused.
        (False, True, "ctrader", True, True, False),
        # Demo-only gate removed -> live account refused.
        (True, False, "ctrader", True, True, False),
        # Broker table gate: alpaca can never run it.
        (True, True, "alpaca", True, True, False),
        # Adapter capability gate removed -> refused.
        (True, True, "ctrader", False, True, False),
        # cTrader deliberately declares no ``supports_stop_amendment`` (the
        # flip workflow uses its own position-addressed amend primitives),
        # so the mode is still admitted - pinned so a future "fix" that
        # couples them breaks loudly here.
        (True, True, "ctrader", True, False, True),
    ],
)
def test_flip_mode_admission_matrix(
    opted: bool,
    demo: bool,
    broker: str,
    flip_capable: bool,
    amend: bool,
    expected: bool,
) -> None:
    """Each gate independently refuses; removing any one must fail here."""
    import live_runner.daemon as daemon
    from shared.trailing import TRAILING_MODE_SUPERTREND_FLIP

    adapter = _flip_gate_adapter(
        broker_name=broker,
        supports_supertrend_flip=flip_capable,
        supports_stop_amendment=amend,
        demo=demo,
    )
    deployment = _flip_gate_deployment(opted=opted)

    modes = daemon._live_signal_trailing_modes(adapter, deployment)

    assert (TRAILING_MODE_SUPERTREND_FLIP in modes) is expected


# ── Stop-only flip entries (#168 maintainer default) ────────────────────────


def _flip_entry_parts(database, *, opted: bool = True, flip: bool = True):
    database.ensure_live_account(ACCOUNT, "ctrader")
    settings = {"live_supertrend_flip": True} if opted else {}
    deployment = LiveDeploymentConfig(
        id=uuid4(),
        strategy_name="flip-entry",
        account_id=ACCOUNT,
        symbols=[SYMBOL],
        timeframe="1m",
        status="ACTIVE",
        signal_source="internal",
        settings=settings,
    )
    database.create_live_deployment(deployment)
    metadata = {
        "stop_loss": STOP_PRICE,
        "take_profit": TP1_PRICE,
        "tp1_price": TP1_PRICE,
        "tp1_exit_percent": TP1_FRACTION,
    }
    if flip:
        metadata["trailing_sl_config"] = {
            "mode": "supertrend_flip",
            "breakeven_after_tp1": True,
            "activation_phase": "wait_for_first_green_after_tp1",
            "exit_phase": "exit_on_first_red_after_activation",
            "supertrend_period": 2,
            "supertrend_multiplier": 2.0,
        }
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol=SYMBOL,
        action="buy",
        theoretical_price=ENTRY_PRICE,
        timestamp=T0,
        metadata=metadata,
    )
    database.save_live_signal(signal)
    return deployment, signal


def test_flip_entry_submits_stop_only_with_unbounded_policy(
    isolated_db,
) -> None:
    """An admitted flip entry carries NO broker TP; the manager owns TP1."""
    import live_runner.daemon as daemon
    from live_runner.base_adapter import UNBOUNDED_TP_POLICY

    database, _engine, _data_dir = isolated_db
    deployment, signal = _flip_entry_parts(database)
    adapter = _FakeFlipAdapter()
    adapter.positions = []

    prepared, _qty = asyncio.run(
        daemon.prepare_order_with_risk(adapter, deployment, signal)
    )

    metadata = prepared.metadata
    assert "take_profit" not in metadata
    assert "tp_price" not in metadata
    assert metadata.get("take_profit_policy") == UNBOUNDED_TP_POLICY
    # The manager's own levels survive: protective stop and TP1 metadata.
    assert metadata.get("stop_loss") == STOP_PRICE
    assert metadata.get("tp1_price") == TP1_PRICE
    assert metadata.get("tp1_exit_percent") == TP1_FRACTION
    assert "supertrend_flip" in str(metadata.get("trailing_sl_config"))


def test_non_flip_entry_keeps_its_broker_take_profit(isolated_db) -> None:
    """Control: a plain entry is untouched by the flip stop-only rule."""
    import live_runner.daemon as daemon

    database, _engine, _data_dir = isolated_db
    deployment, signal = _flip_entry_parts(database, flip=False)
    adapter = _FakeFlipAdapter()
    adapter.positions = []

    prepared, _qty = asyncio.run(
        daemon.prepare_order_with_risk(adapter, deployment, signal)
    )

    assert prepared.metadata.get("take_profit") == TP1_PRICE


def test_flip_entry_refused_without_opt_in(isolated_db) -> None:
    """The opt-in gate also guards submission, not only the manager."""
    import live_runner.daemon as daemon

    database, _engine, _data_dir = isolated_db
    deployment, signal = _flip_entry_parts(database, opted=False)
    adapter = _FakeFlipAdapter()
    adapter.positions = []

    with pytest.raises(ValueError):
        asyncio.run(
            daemon.prepare_order_with_risk(adapter, deployment, signal)
        )


def test_flip_entry_refused_on_live_account(isolated_db) -> None:
    """The demo-only gate refuses a flip entry on a live cTrader account."""
    import live_runner.daemon as daemon

    database, _engine, _data_dir = isolated_db
    deployment, signal = _flip_entry_parts(database)
    adapter = _FakeFlipAdapter()
    adapter.demo = False
    adapter.positions = []

    with pytest.raises(ValueError):
        asyncio.run(
            daemon.prepare_order_with_risk(adapter, deployment, signal)
        )
