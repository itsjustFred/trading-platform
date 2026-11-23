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

    # ── BaseLiveAdapter abstracts ────────────────────────────────────────

    async def connect(self) -> None:
        return None

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return (100_000.0, 100_000.0)

    @property
    def supports_stop_amendment(self) -> bool:
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
        return LiveQuote(bid=self.bid, ask=self.bid + 0.0001)

    def supports_market_data_timeframe(self, timeframe: str) -> bool:
        return True

    async def get_market_data(self, symbol, *, timeframe, limit=200):
        return self.frame

    async def get_order_state(self, order_id: str, *, client_order_id: str = ""):
        return self.order_states.get(str(order_id))

    def position_qty_tolerance(self, symbol: str) -> float:
        return 1e-9

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
        def supports_stop_amendment(self) -> bool:
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
