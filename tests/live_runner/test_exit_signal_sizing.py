"""#121: an exit/close message closes the deployment's own quantity, nothing more.

Maintainer decisions encoded here:

1. Exit/close -> exit-only order; the side comes from the position. No position
   -> ignore and log; nothing reaches the broker.
2. Netting: an exit closes **only the deployment's own quantity** from the ledger
   (its persisted fills). Another deployment's share of the same netted broker
   position stays open. The close is capped at the broker position, and a ledger
   larger than the broker position is logged.
3. Partial exits close that fraction of the deployment's own quantity.

The exit-only representation is dispatched independently of entries: it never
goes through an entry signal processor (Nitro is BUY-only and would refuse it)
and never becomes a new short on a deployment that allows shorting.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from live_runner.base_adapter import LivePipValue
from risk import TradeSignal
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal


def _instruction(fraction: float = 1.0, side: str | None = None):
    from live_runner.exit_signals import ExitInstruction

    return ExitInstruction(fraction=fraction, position_side=side)


def _resolve(ledger_qty, positions, *, fraction=1.0, side=None, symbol="AAPL"):
    from live_runner.exit_signals import resolve_exit_order

    return resolve_exit_order(
        symbol=symbol,
        ledger_qty=ledger_qty,
        broker_positions=positions,
        instruction=_instruction(fraction, side),
    )


# ─── The sizing rule, as a pure function ─────────────────────────────────────


def test_full_exit_closes_the_own_ledger_quantity() -> None:
    assert _resolve(10.0, [{"symbol": "AAPL", "qty": 10.0}]) == ("sell", 10.0)


def test_netted_position_keeps_the_other_deployments_share_open() -> None:
    """Broker holds 25 = 10 (this deployment) + 15 (another): close 10 only."""
    assert _resolve(10.0, [{"symbol": "AAPL", "qty": 25.0}]) == ("sell", 10.0)


def test_ledger_above_the_broker_position_is_capped_and_logged(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert _resolve(30.0, [{"symbol": "AAPL", "qty": 25.0}]) == ("sell", 25.0)
    assert any("exceeds the broker position" in record.getMessage() for record in caplog.records)


def test_a_short_is_closed_with_a_buy() -> None:
    assert _resolve(-8.0, [{"symbol": "AAPL", "qty": -8.0}]) == ("buy", 8.0)


@pytest.mark.parametrize(
    ("ledger_qty", "positions"),
    [
        (10.0, []),
        (10.0, [{"symbol": "MSFT", "qty": 10.0}]),
        (0.0, [{"symbol": "AAPL", "qty": 10.0}]),
    ],
    ids=["no-broker-position", "other-symbol-only", "no-own-quantity"],
)
def test_nothing_to_close_is_ignored(ledger_qty, positions) -> None:
    from live_runner.exit_signals import NothingToExitError

    with pytest.raises(NothingToExitError):
        _resolve(ledger_qty, positions)


def test_own_side_disagreeing_with_the_broker_is_refused() -> None:
    """Own long 10 on a broker book that is net short: selling would extend the
    short. Refuse rather than guess."""
    from live_runner.exit_signals import NothingToExitError

    with pytest.raises(ValueError) as excinfo:
        _resolve(10.0, [{"symbol": "AAPL", "qty": -5.0}])
    assert not isinstance(excinfo.value, NothingToExitError)


def test_the_named_side_must_match_the_own_position() -> None:
    """"Exit long AAPL" must never close (buy back) a short."""
    with pytest.raises(ValueError, match="long"):
        _resolve(-10.0, [{"symbol": "AAPL", "qty": -10.0}], side="long")
    assert _resolve(10.0, [{"symbol": "AAPL", "qty": 10.0}], side="long") == ("sell", 10.0)


def test_mixed_or_duplicate_broker_rows_are_refused() -> None:
    with pytest.raises(ValueError):
        _resolve(
            10.0,
            [{"symbol": "AAPL", "qty": 10.0}, {"symbol": "AAPL", "qty": 5.0}],
        )


@pytest.mark.parametrize(
    ("ledger_qty", "fraction", "expected"),
    [
        (10.0, 0.5, 5.0),
        (10.0, 1 / 3, 3.0),
        (9.0, 1 / 3, 3.0),
        (10.0, 0.25, 2.0),
        (0.5, 0.5, 0.25),
    ],
)
def test_partial_exit_closes_that_fraction_of_the_own_quantity(
    ledger_qty, fraction, expected
) -> None:
    action, qty = _resolve(
        ledger_qty, [{"symbol": "AAPL", "qty": 100.0}], fraction=fraction
    )
    assert action == "sell"
    assert qty == pytest.approx(expected)


def test_partial_of_the_capped_quantity() -> None:
    """The fraction applies to what this deployment can actually close."""
    assert _resolve(
        30.0, [{"symbol": "AAPL", "qty": 20.0}], fraction=0.5
    ) == ("sell", 10.0)


def test_a_partial_that_rounds_to_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="partial"):
        _resolve(2.0, [{"symbol": "AAPL", "qty": 2.0}], fraction=1 / 3)


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5, float("nan"), float("inf")])
def test_an_invalid_fraction_is_refused(fraction) -> None:
    from live_runner.exit_signals import ExitInstruction

    with pytest.raises(ValueError):
        ExitInstruction(fraction=fraction, position_side=None)


def test_exit_instruction_round_trips_through_metadata() -> None:
    from live_runner.exit_signals import exit_instruction_from_metadata, is_exit_intent

    metadata = {"signal_intent": "exit", "exit_fraction": 0.5, "exit_position_side": "long"}
    assert is_exit_intent(metadata)
    instruction = exit_instruction_from_metadata(metadata)
    assert instruction.fraction == 0.5
    assert instruction.position_side == "long"
    assert not is_exit_intent({"exit_only": True})
    with pytest.raises(ValueError):
        exit_instruction_from_metadata({"signal_intent": "exit"})
    with pytest.raises(ValueError):
        exit_instruction_from_metadata(
            {"signal_intent": "exit", "exit_fraction": 1.0, "exit_position_side": "sideways"}
        )


# ─── The ledger: this deployment's own fills, per symbol ─────────────────────


def _save_signal_with_fill(database, *, deployment_id, symbol, action, qty):
    signal = LiveSignal(
        id=uuid.uuid4(),
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"idempotency_key": uuid.uuid4().hex},
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    database.save_live_execution(
        LiveExecution(
            signal_id=uuid.UUID(signal_id),
            actual_price=100.0,
            filled_qty=qty,
            commission=0.0,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=1),
            broker_order_id=f"order-{uuid.uuid4().hex[:8]}",
        )
    )


def test_ledger_nets_only_this_deployments_fills(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    other = str(uuid.uuid4())
    seed_live_deployments(database, [mine, other])

    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="buy", qty=10.0)
    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="buy", qty=5.0)
    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="sell", qty=4.0)
    _save_signal_with_fill(database, deployment_id=mine, symbol="MSFT", action="buy", qty=7.0)
    _save_signal_with_fill(database, deployment_id=other, symbol="AAPL", action="buy", qty=100.0)

    assert database.get_live_deployment_net_filled_qty(mine, "AAPL") == pytest.approx(11.0)
    assert database.get_live_deployment_net_filled_qty(mine, "aapl") == pytest.approx(11.0)
    assert database.get_live_deployment_net_filled_qty(mine, "MSFT") == pytest.approx(7.0)
    assert database.get_live_deployment_net_filled_qty(other, "AAPL") == pytest.approx(100.0)
    assert database.get_live_deployment_net_filled_qty(mine, "TSLA") == 0.0


def test_ledger_reports_a_short_as_negative(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])
    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="sell", qty=6.0)

    assert database.get_live_deployment_net_filled_qty(mine, "AAPL") == pytest.approx(-6.0)


# ─── Through the daemon ──────────────────────────────────────────────────────


def _deployment(*, allow_short: bool = False, processor: str | None = None) -> LiveDeploymentConfig:
    signal_config: dict[str, object] = {"channel_id": "100000000000000121"}
    if processor:
        signal_config["signal_processor"] = processor
    return LiveDeploymentConfig(
        id=str(uuid.uuid4()),
        strategy_name="discord::121",
        account_id="acc-121",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="discord",
        signal_source_type="discord",
        signal_config=signal_config,
        settings={"allow_short": allow_short},
    )


def _exit_trade_signal(*, fraction: float = 1.0, side: str | None = "long") -> TradeSignal:
    metadata: dict[str, object] = {
        "source": "discord",
        "signal_intent": "exit",
        "exit_only": True,
        "risk_sizing_mode": "exit_only",
        "exit_fraction": fraction,
        "discord_channel_id": "100000000000000121",
        "discord_message_id": "900000000000000121",
        "discord_author_id": "bot-121",
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    if side is not None:
        metadata["exit_position_side"] = side
    return TradeSignal(
        action="sell",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata=metadata,
    )


class _Adapter:
    def __init__(self, positions):
        self.positions = positions
        self.submitted: list[tuple[LiveSignal, float]] = []

    async def get_positions(self):
        return list(self.positions)

    async def get_balance(self):
        return 100_000.0, 100_000.0

    async def get_equity(self):
        return 100_000.0

    async def get_used_margin(self):
        return 0.0

    async def get_pip_value(self, symbol):
        # Entries fail closed without a broker pip value (#146).
        return LivePipValue(
            value=1.0,
            units_per_qty=1.0,
            conversion_rate=1.0,
            quote_asset="USD",
            account_asset="USD",
            source="test",
        )

    async def submit_order(self, signal, *, qty=None):
        self.submitted.append((signal, qty))
        return "order-121"


_OTHER = "00000000-0000-0000-0000-0000000000b2"


def _patch_ledger(
    monkeypatch,
    deployment: LiveDeploymentConfig,
    qty: float,
    *,
    others: dict[str, float] | None = None,
    unfilled_exits: list[dict] | None = None,
    other_unfilled_exits: list[dict] | None = None,
) -> list[tuple[str, str]]:
    """Ledgers without a database.

    ``qty`` is this deployment's own net filled quantity, ``others`` the other
    deployments' on the same account. ``unfilled_exits`` are this deployment's
    earlier exit orders not known to be fully filled, ``other_unfilled_exits`` the
    same for the other deployments on the account. Returns the
    ``(account_id, symbol)`` lookups of the account ledger.
    """
    import live_runner.daemon as daemon

    calls: list[tuple[str, str]] = []
    ledgers = {str(deployment.id): qty, **(others or {})}

    def _account_ledgers(account_id: str, symbol: str) -> dict[str, float]:
        calls.append((account_id, symbol))
        return dict(ledgers)

    def _unfilled_exits(deployment_id: str, symbol: str) -> list[dict]:
        return list(unfilled_exits or [])

    def _account_unfilled_exits(account_id: str, symbol: str, **_kwargs) -> list[dict]:
        return list(other_unfilled_exits or [])

    monkeypatch.setattr(
        daemon, "get_account_net_filled_qty_by_deployment", _account_ledgers, raising=False
    )
    monkeypatch.setattr(
        daemon, "list_live_deployment_unfilled_exit_orders", _unfilled_exits, raising=False
    )
    monkeypatch.setattr(
        daemon, "list_account_unfilled_exit_orders", _account_unfilled_exits, raising=False
    )
    return calls


def test_exit_signal_is_exit_only_even_when_shorting_is_allowed() -> None:
    """On a long+short deployment a plain sell is a real short. An exit is not."""
    from live_runner.daemon import _trade_signal_to_live_signal

    live = _trade_signal_to_live_signal(_exit_trade_signal(), _deployment(allow_short=True))
    assert live.metadata["exit_only"] is True
    assert live.metadata["risk_sizing_mode"] == "exit_only"
    assert live.metadata["signal_intent"] == "exit"


def test_prepare_sizes_an_exit_from_the_own_ledger(monkeypatch) -> None:
    from live_runner.daemon import _trade_signal_to_live_signal, prepare_order_with_risk

    deployment = _deployment(allow_short=True)
    calls = _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: 15.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 25.0}])
    live = _trade_signal_to_live_signal(_exit_trade_signal(), deployment)

    prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert calls == [(deployment.account_id, "AAPL")]
    assert qty == pytest.approx(10.0)
    assert prepared.action == "sell"
    assert prepared.metadata["qty"] == pytest.approx(10.0)
    assert prepared.metadata["explicit_qty"] == pytest.approx(10.0)
    assert prepared.metadata["risk_sizing_mode"] == "exit_only"
    # No entry levels: the adapter builds a plain close, not a bracket.
    assert "stop_loss" not in prepared.metadata
    assert "take_profit" not in prepared.metadata


def test_exit_drops_spoofed_entry_gate_fields(monkeypatch) -> None:
    """The entry-deviation gate never runs on an exit, so a payload can't stamp it.

    Entries overwrite these keys with the gate's own figures; an exit has no gate
    result, so a payload value under one of them would otherwise be persisted as
    if the gate had accepted it.
    """
    from live_runner.daemon import (
        _ENTRY_QUOTE_METADATA_KEYS,
        _trade_signal_to_live_signal,
        prepare_order_with_risk,
    )

    deployment = _deployment(allow_short=True)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 10.0}])
    trade_signal = _exit_trade_signal()
    spoofed = {key: "spoofed" for key in _ENTRY_QUOTE_METADATA_KEYS}
    trade_signal = dataclasses.replace(
        trade_signal, metadata={**trade_signal.metadata, **spoofed}
    )
    live = _trade_signal_to_live_signal(trade_signal, deployment)
    assert all(live.metadata.get(key) == "spoofed" for key in _ENTRY_QUOTE_METADATA_KEYS)

    prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert qty == pytest.approx(10.0)
    for key in _ENTRY_QUOTE_METADATA_KEYS:
        assert key not in prepared.metadata, key


def test_prepare_takes_the_close_side_from_the_position(monkeypatch) -> None:
    from live_runner.daemon import _trade_signal_to_live_signal, prepare_order_with_risk

    deployment = _deployment(allow_short=True)
    _patch_ledger(monkeypatch, deployment, -4.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": -4.0}])
    live = _trade_signal_to_live_signal(_exit_trade_signal(side=None), deployment)

    prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert prepared.action == "buy"
    assert qty == pytest.approx(4.0)


def test_prepare_partial_exit(monkeypatch) -> None:
    from live_runner.daemon import _trade_signal_to_live_signal, prepare_order_with_risk

    deployment = _deployment()
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 10.0}])
    live = _trade_signal_to_live_signal(_exit_trade_signal(fraction=0.5), deployment)

    _prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert qty == pytest.approx(5.0)


def _run_submit(adapter, deployment, trade_signal) -> None:
    import live_runner.daemon as daemon

    async def _no_wait(_signal, _timeframe):
        return None

    original_wait = daemon.wait_for_candle_close
    original_can_dispatch = daemon._deployment_can_dispatch
    original_claim = daemon.claim_live_deployment_submission
    original_release = daemon.release_live_deployment_submission
    daemon.wait_for_candle_close = _no_wait
    daemon._deployment_can_dispatch = lambda _deployment, **_kwargs: True
    daemon.claim_live_deployment_submission = lambda *_a, **_k: True
    daemon.release_live_deployment_submission = lambda *_a, **_k: True
    try:
        asyncio.run(
            daemon._submit_normalized_discord_signal(
                deployment.account_id,
                adapter,
                deployment,
                trade_signal,
                signal_timeframe="1h",
                wait_enabled=False,
            )
        )
    finally:
        daemon.wait_for_candle_close = original_wait
        daemon._deployment_can_dispatch = original_can_dispatch
        daemon.claim_live_deployment_submission = original_claim
        daemon.release_live_deployment_submission = original_release


def test_exit_without_a_position_is_ignored_and_logged(monkeypatch, caplog) -> None:
    deployment = _deployment()
    _patch_ledger(monkeypatch, deployment, 0.0)
    adapter = _Adapter([])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert adapter.submitted == []
    assert any("Exit signal ignored" in record.getMessage() for record in caplog.records)


def test_exit_with_a_position_is_submitted_as_exit_only(monkeypatch) -> None:
    deployment = _deployment()
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 10.0}])

    _run_submit(adapter, deployment, _exit_trade_signal())

    assert len(adapter.submitted) == 1
    signal, qty = adapter.submitted[0]
    assert signal.action == "sell"
    assert qty == pytest.approx(10.0)
    assert signal.metadata["risk_sizing_mode"] == "exit_only"


def test_exit_bypasses_the_entry_signal_processor(monkeypatch) -> None:
    """Nitro's processor is BUY-only and needs market data: running an exit
    through it would silently drop every close on a Nitro deployment."""
    import live_runner.daemon as daemon

    def _processor_must_not_run(*_args, **_kwargs):
        raise AssertionError("an exit must not be run through an entry processor")

    monkeypatch.setattr(daemon, "get_signal_processor", _processor_must_not_run)
    deployment = _deployment(processor="nitro")
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 10.0}])

    _run_submit(adapter, deployment, _exit_trade_signal())

    assert len(adapter.submitted) == 1


def test_parsed_exit_message_end_to_end(monkeypatch) -> None:
    """The issue's first row, from Discord text to the broker call."""
    from types import SimpleNamespace

    from live_runner.signal_sources.discord_source import _parse_trade_signal_from_message

    message = SimpleNamespace(
        id="900000000000000122",
        content="Exit long AAPL @ 100",
        channel=SimpleNamespace(id="100000000000000121"),
        author=SimpleNamespace(id="bot-121", bot=True),
        created_at=datetime.now(timezone.utc),
    )
    trade_signal = _parse_trade_signal_from_message(message, default_timeframe="1h")
    assert trade_signal is not None

    deployment = _deployment(allow_short=True)
    _patch_ledger(monkeypatch, deployment, 3.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 3.0}])

    _run_submit(adapter, deployment, trade_signal)

    assert len(adapter.submitted) == 1
    signal, qty = adapter.submitted[0]
    assert signal.action == "sell", "an exit of a long must sell, never buy"
    assert qty == pytest.approx(3.0)


# ─── Plain and momentum-reversal SELLs are exits too ─────────────────────────
#
# Follow-up decision on #121: *every* exit closes only the deployment's own
# quantity, not just parsed exit/close messages. A plain SELL or a RocketFuel
# momentum-reversal SELL on a deployment that cannot short used to flatten the
# whole netted broker position, closing other deployments' shares with it.


def _sell_trade_signal(**extra_metadata: object) -> TradeSignal:
    metadata: dict[str, object] = {
        "source": "discord",
        "discord_channel_id": "100000000000000121",
        "discord_message_id": "900000000000000123",
        "discord_author_id": "bot-121",
        "received_at": datetime.now(timezone.utc).isoformat(),
        **extra_metadata,
    }
    return TradeSignal(
        action="sell",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata=metadata,
    )


def _reversal_trade_signal() -> TradeSignal:
    """What RocketFuel's long-only processor emits for a momentum reversal."""
    return _sell_trade_signal(
        exit_only=True,
        signal_type="momentum_reversal",
        signal_bot="rocketfuel",
        rocketfuel_variant="longonly",
        processor_mode="long-only-exit-on-sell",
        take_profit_policy="unbounded_exit_on_opposite_signal",
    )


@pytest.mark.parametrize(
    "trade_signal",
    [_sell_trade_signal(), _reversal_trade_signal()],
    ids=["plain-sell", "momentum-reversal"],
)
def test_a_sell_exit_keeps_the_other_deployments_share_open(
    monkeypatch, trade_signal
) -> None:
    """Broker holds 25 = 10 (this deployment) + 15 (another): sell 10 only."""
    deployment = _deployment(allow_short=False)
    calls = _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: 15.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 25.0}])

    _run_submit(adapter, deployment, trade_signal)

    assert calls == [(deployment.account_id, "AAPL")]
    assert len(adapter.submitted) == 1
    signal, qty = adapter.submitted[0]
    assert signal.action == "sell"
    assert qty == pytest.approx(10.0)
    assert signal.metadata["explicit_qty"] == pytest.approx(10.0)
    assert signal.metadata["risk_sizing_mode"] == "exit_only"
    # Recorded as the full exit it was executed as.
    assert signal.metadata["signal_intent"] == "exit"
    assert signal.metadata["exit_fraction"] == pytest.approx(1.0)
    assert "stop_loss" not in signal.metadata
    assert "take_profit" not in signal.metadata


@pytest.mark.parametrize(
    "trade_signal",
    [_sell_trade_signal(), _reversal_trade_signal()],
    ids=["plain-sell", "momentum-reversal"],
)
def test_a_sell_exit_without_own_quantity_is_ignored(
    monkeypatch, caplog, trade_signal
) -> None:
    """The broker position is entirely another deployment's: close nothing."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 0.0, others={_OTHER: 25.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 25.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, trade_signal)

    assert adapter.submitted == []
    assert any("Exit signal ignored" in record.getMessage() for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_a_sell_exit_whose_ledger_exceeds_the_broker_is_capped_and_alerts(
    monkeypatch, caplog
) -> None:
    """#121 decision 2: a ledger above the broker position closes at most the
    broker quantity, and the mismatch raises an operator alert."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 30.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 12.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [12.0]
    alerts = _alerts(caplog)
    assert len(alerts) == 1
    assert alerts[0].data["own_ledger_qty"] == pytest.approx(30.0)
    assert alerts[0].data["broker_qty"] == pytest.approx(12.0)
    assert "another deployment" in alerts[0].getMessage()


def test_a_partial_exit_of_an_overhanging_ledger_is_a_fraction_of_the_cap(
    monkeypatch, caplog
) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 30.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 12.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal(fraction=0.5))

    assert [qty for _, qty in adapter.submitted] == [6.0]
    assert len(_alerts(caplog)) == 1


def test_matching_ledgers_raise_no_alert(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: 5.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 15.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [10.0]
    assert _alerts(caplog) == []


def test_an_exit_against_the_opposite_broker_side_is_still_refused(
    monkeypatch, caplog
) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": -10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert adapter.submitted == []
    assert _records_at(caplog, logging.ERROR, "Exit signal refused before submission")


def test_an_exit_capped_to_nothing_is_ignored_with_a_warning(monkeypatch, caplog) -> None:
    """Own 10, broker 0.4 of a whole-unit step: nothing left after flooring."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _SteppedAdapter([{"symbol": "AAPL", "qty": 0.4}], step=1.0)

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert adapter.submitted == []
    assert _records_at(caplog, logging.WARNING, "Exit signal ignored")


def test_a_sell_exit_of_an_own_short_buys_it_back(monkeypatch) -> None:
    """#76 still holds: the close side comes from the own position."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, -3.0, others={_OTHER: -5.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": -8.0}])

    _run_submit(adapter, deployment, _sell_trade_signal())

    assert len(adapter.submitted) == 1
    signal, qty = adapter.submitted[0]
    assert signal.action == "buy"
    assert qty == pytest.approx(3.0)


def test_a_sell_on_a_long_and_short_deployment_is_still_an_entry() -> None:
    """Only a deployment that cannot short turns a plain SELL into an exit."""
    from live_runner.daemon import _trade_signal_to_live_signal
    from live_runner.exit_signals import is_exit_order

    live = _trade_signal_to_live_signal(
        _sell_trade_signal(), _deployment(allow_short=True)
    )
    assert not is_exit_order(live.action, live.metadata)


def test_is_exit_order_covers_every_exit_and_nothing_else() -> None:
    from live_runner.exit_signals import is_exit_order

    assert is_exit_order("sell", {"signal_intent": "exit", "exit_fraction": 1.0})
    # The action of a parsed exit is a placeholder; the intent decides.
    assert is_exit_order("buy", {"signal_intent": "exit", "exit_fraction": 1.0})
    assert is_exit_order("sell", {"exit_only": True})
    assert is_exit_order("SELL", {"exit_only": True})
    assert not is_exit_order("sell", {})
    assert not is_exit_order("sell", None)
    assert not is_exit_order("sell", {"exit_only": False})
    # exit_only on a buy is not a path the daemon sizes as an exit.
    assert not is_exit_order("buy", {"exit_only": True})


# ─── A refused exit is an ERROR: the position stays open ─────────────────────


class _RefusingAdapter(_Adapter):
    """Refuses every order before the broker request, like a broker-side
    volume floor or Alpaca's no-position guard."""

    # A provable demo/paper account: an unpromoted entry on real money is refused
    # by the promotion gate before it reaches the broker (#165), and these tests
    # are about the broker's own refusal.
    demo = True

    async def get_quote(self, symbol):
        # Entries need a fresh broker quote for the #125 deviation gate.
        from datetime import datetime, timezone

        from live_runner.base_adapter import LiveQuote

        return LiveQuote(
            bid=100.0,
            ask=100.0,
            timestamp=datetime.now(timezone.utc),
            source="test",
            timestamp_source="broker",
        )

    async def submit_order(self, signal, *, qty=None):
        from live_runner.base_adapter import order_not_submitted

        self.submitted.append((signal, qty))
        with order_not_submitted():
            raise ValueError("order rejected: volume below the broker minimum")


class _UnknownOutcomeAdapter(_Adapter):
    """The broker answered with a rejection after the request went out."""

    async def submit_order(self, signal, *, qty=None):
        self.submitted.append((signal, qty))
        raise RuntimeError("Order rejected (MARKET_CLOSED): market is closed")


def _alerts(caplog):
    """The operator alerts: ERROR records marked ``exit_ledger_mismatch``."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and getattr(record, "data", {}).get("alert") == "exit_ledger_mismatch"
    ]


def _records_at(caplog, level: int, text: str):
    return [
        record
        for record in caplog.records
        if record.levelno == level and text in record.getMessage()
    ]


@pytest.mark.parametrize(
    "trade_signal",
    [_exit_trade_signal(), _sell_trade_signal(), _reversal_trade_signal()],
    ids=["exit-message", "plain-sell", "momentum-reversal"],
)
def test_a_broker_refused_exit_is_logged_at_error(monkeypatch, caplog, trade_signal) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _RefusingAdapter([{"symbol": "AAPL", "qty": 10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, trade_signal)

    assert len(adapter.submitted) == 1
    assert _records_at(caplog, logging.ERROR, "Exit signal was refused before the broker request")
    assert not _records_at(caplog, logging.WARNING, "refused before the broker request")


def test_a_broker_rejection_after_the_request_is_logged_at_error(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _UnknownOutcomeAdapter([{"symbol": "AAPL", "qty": 10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    errors = _records_at(caplog, logging.ERROR, "unknown outcome")
    assert errors
    assert errors[0].data["exit"] is True


def test_an_exit_refused_before_submission_is_logged_at_error(monkeypatch, caplog) -> None:
    """Own long on a net-short broker book: refused, nothing sent, ERROR."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": -5.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert adapter.submitted == []
    assert _records_at(caplog, logging.ERROR, "Exit signal refused before submission")


def test_a_refused_entry_is_still_only_a_warning(monkeypatch, caplog) -> None:
    """Refusing an entry opens nothing; it must not page anyone."""
    deployment = _deployment(allow_short=True)
    adapter = _RefusingAdapter([])
    entry = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        timeframe="1h",
        metadata=dict(_sell_trade_signal().metadata),
    )

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, entry)

    assert len(adapter.submitted) == 1
    assert _records_at(
        caplog, logging.WARNING, "Discord signal was refused before the broker request"
    )
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_a_sell_exit_records_no_side_it_did_not_execute(monkeypatch) -> None:
    """A stray exit_position_side on a plain SELL is not what ran: drop it."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, -3.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": -3.0}])

    _run_submit(adapter, deployment, _sell_trade_signal(exit_position_side="long"))

    assert len(adapter.submitted) == 1
    signal, _qty = adapter.submitted[0]
    assert signal.action == "buy"
    assert "exit_position_side" not in signal.metadata


# ─── Review finding 2: the ledgers must reconcile with the broker ────────────
#
# The ledger only holds fills persisted against a deployment's own signals.
# Broker-side closes (a bracket's stop or target leg, a cTrader SL/TP, a flatten,
# a manual close) never reach it. On a netted account that let one deployment's
# exit close another's shares. Before any exit is sized, the signed ledgers of
# every deployment on the account must add up to the broker position.


def test_a_stop_fill_missing_from_the_ledger_submits_the_capped_exit_and_alerts(
    monkeypatch, caplog
) -> None:
    """A's stop filled at the broker, so A's ledger still says +10 while the
    broker holds only B's 5. A's exit is capped at 5 and the alert says what
    does not add up (and that this 5 may be B's share)."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: 5.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 5.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [5.0]
    alerts = _alerts(caplog)
    assert len(alerts) == 1, [record.getMessage() for record in caplog.records]
    data = alerts[0].data
    assert data["ledger_qty_by_deployment"] == {str(deployment.id): 10.0, _OTHER: 5.0}
    assert data["ledger_qty_total"] == pytest.approx(15.0)
    assert data["broker_qty"] == pytest.approx(5.0)
    assert "another deployment" in alerts[0].getMessage()


@pytest.mark.parametrize(
    "trade_signal",
    [_exit_trade_signal(), _sell_trade_signal(), _reversal_trade_signal()],
    ids=["exit-message", "plain-sell", "momentum-reversal"],
)
def test_every_exit_kind_alerts_and_is_capped(monkeypatch, caplog, trade_signal) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: 5.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 5.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, trade_signal)

    assert [qty for _, qty in adapter.submitted] == [5.0]
    assert len(_alerts(caplog)) == 1


def test_a_ledger_short_of_the_broker_closes_only_the_own_qty_and_alerts(
    monkeypatch, caplog
) -> None:
    """Another deployment or a manual fill holds the rest: own quantity only."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: 5.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 20.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [10.0]
    assert len(_alerts(caplog)) == 1


class _ToleranceAdapter(_Adapter):
    """An adapter that knows its instrument's quantity step."""

    def __init__(self, positions, tolerance: float):
        super().__init__(positions)
        self.tolerance = tolerance
        self.tolerance_symbols: list[str] = []

    def position_qty_tolerance(self, symbol: str) -> float:
        self.tolerance_symbols.append(symbol)
        return self.tolerance


def test_ledgers_reconcile_within_the_instrument_precision(monkeypatch) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 1.0004, others={_OTHER: 2.0})
    adapter = _ToleranceAdapter([{"symbol": "AAPL", "qty": 3.0}], tolerance=0.0005)

    _run_submit(adapter, deployment, _sell_trade_signal())

    assert adapter.tolerance_symbols == ["AAPL"]
    assert len(adapter.submitted) == 1


def test_ledgers_outside_the_instrument_precision_alert(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 1.0006, others={_OTHER: 2.0})
    adapter = _ToleranceAdapter([{"symbol": "AAPL", "qty": 3.0}], tolerance=0.0005)

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [pytest.approx(1.0006)]
    assert len(_alerts(caplog)) == 1


def test_without_a_known_precision_the_match_is_strict(monkeypatch, caplog) -> None:
    """No precision from the adapter: fail closed on anything above 1e-9."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 1.000001, others={_OTHER: 2.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 3.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [pytest.approx(1.000001)]
    assert len(_alerts(caplog)) == 1


@pytest.mark.parametrize("tolerance", [float("nan"), float("inf"), -1.0, "x", 1.5, 100.0])
def test_an_unusable_precision_falls_back_to_strict(monkeypatch, caplog, tolerance) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 1.000001, others={_OTHER: 2.0})
    adapter = _ToleranceAdapter([{"symbol": "AAPL", "qty": 3.0}], tolerance=tolerance)

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert len(adapter.submitted) == 1
    assert len(_alerts(caplog)) == 1


def test_usable_qty_tolerance_is_bounded_to_one_whole_unit() -> None:
    from live_runner.exit_signals import DEFAULT_QTY_TOLERANCE, usable_qty_tolerance

    assert usable_qty_tolerance(1.0) == 1.0
    assert usable_qty_tolerance(0.5) == 0.5
    assert usable_qty_tolerance(0.0) == 0.0
    assert usable_qty_tolerance(1.0000001) == DEFAULT_QTY_TOLERANCE
    assert usable_qty_tolerance(100.0) == DEFAULT_QTY_TOLERANCE


def test_exit_metadata_uses_the_shared_exit_only_constant(monkeypatch) -> None:
    import live_runner.exit_signals as exit_signals
    from live_runner.exit_signals import ExitInstruction, exit_intent_metadata

    assert exit_intent_metadata(ExitInstruction(fraction=1.0, position_side=None))[
        "risk_sizing_mode"
    ] == "exit_only"
    # The persisted marker follows the constant, not a second spelling.
    monkeypatch.setattr(exit_signals, "EXIT_ONLY_RISK_SIZING_MODE", "sentinel_mode")
    assert exit_intent_metadata(ExitInstruction(fraction=1.0, position_side=None))[
        "risk_sizing_mode"
    ] == "sentinel_mode"


def test_ledgers_must_match_broker_pure() -> None:
    from live_runner.exit_signals import LedgerMismatchError, ensure_ledgers_match_broker

    ensure_ledgers_match_broker(
        symbol="AAPL", ledgers={"a": 10.0, "b": 15.0}, broker_qty=25.0, tolerance=1e-9
    )
    ensure_ledgers_match_broker(
        symbol="AAPL", ledgers={"a": -3.0, "b": -5.0}, broker_qty=-8.0, tolerance=1e-9
    )
    with pytest.raises(LedgerMismatchError) as excinfo:
        ensure_ledgers_match_broker(
            symbol="AAPL", ledgers={"a": 10.0, "b": 5.0}, broker_qty=5.0, tolerance=1e-9
        )
    assert excinfo.value.ledgers == {"a": 10.0, "b": 5.0}
    assert excinfo.value.broker_qty == pytest.approx(5.0)
    assert "a" in str(excinfo.value) and "b" in str(excinfo.value)
    with pytest.raises(LedgerMismatchError):
        ensure_ledgers_match_broker(
            symbol="AAPL", ledgers={"a": 10.0}, broker_qty=10.0, tolerance=float("nan")
        )


def test_account_ledger_sums_every_deployment_on_the_account_only(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine, other, stopped, elsewhere = (str(uuid.uuid4()) for _ in range(4))
    seed_live_deployments(database, [mine, other, stopped], account_id="acc-121")
    seed_live_deployments(database, [elsewhere], account_id="acc-other")

    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="buy", qty=10.0)
    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="sell", qty=4.0)
    _save_signal_with_fill(database, deployment_id=other, symbol="aapl", action="buy", qty=5.0)
    # A deployment's status does not make its holdings disappear from the broker.
    _save_signal_with_fill(database, deployment_id=stopped, symbol="AAPL", action="sell", qty=2.0)
    _save_signal_with_fill(database, deployment_id=mine, symbol="MSFT", action="buy", qty=7.0)
    _save_signal_with_fill(
        database, deployment_id=elsewhere, symbol="AAPL", action="buy", qty=100.0
    )

    ledgers = database.get_account_net_filled_qty_by_deployment("acc-121", "AAPL")

    assert ledgers == {
        mine: pytest.approx(6.0),
        other: pytest.approx(5.0),
        stopped: pytest.approx(-2.0),
    }
    assert database.get_account_net_filled_qty_by_deployment("acc-121", "TSLA") == {}


# ─── Review concern 3: one exit at a time per deployment and symbol ──────────


def test_a_second_exit_waits_for_the_first_to_finish(monkeypatch, caplog) -> None:
    """"Trim half" twice: the second must not size off a ledger that does not
    yet include the first, still working, exit."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch,
        deployment,
        10.0,
        unfilled_exits=[
            {"signal_id": "sig-1", "broker_order_id": "ord-1", "requested_qty": 5.0,
             "filled_qty": 0.0},
        ],
    )

    class _WorkingAdapter(_Adapter):
        async def working_order_ids(self):
            return {"ord-1"}

    adapter = _WorkingAdapter([{"symbol": "AAPL", "qty": 10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal(fraction=0.5))

    assert adapter.submitted == []
    # The refused exit leaves a position open: ERROR (#142 review), not WARNING.
    errors = _records_at(caplog, logging.ERROR, "previous exit")
    assert errors, [record.getMessage() for record in caplog.records]
    assert "this deployment" in errors[0].data["reason"]
    assert not _records_at(caplog, logging.WARNING, "previous exit")


def test_an_exit_still_working_under_its_client_order_id_blocks_too(monkeypatch) -> None:
    """The broker id can be missing (response lost); Alpaca's client order id
    is the signal id."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch,
        deployment,
        10.0,
        unfilled_exits=[
            {"signal_id": "sig-1", "broker_order_id": "", "requested_qty": 5.0,
             "filled_qty": 0.0},
        ],
    )

    class _WorkingAdapter(_Adapter):
        async def working_order_ids(self):
            return {"sig-1"}

    adapter = _WorkingAdapter([{"symbol": "AAPL", "qty": 10.0}])

    _run_submit(adapter, deployment, _sell_trade_signal())

    assert adapter.submitted == []


def test_a_terminal_previous_exit_does_not_block(monkeypatch) -> None:
    """Cancelled or rejected (not working, not fully filled): go ahead."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch,
        deployment,
        10.0,
        unfilled_exits=[
            {"signal_id": "sig-1", "broker_order_id": "ord-1", "requested_qty": 5.0,
             "filled_qty": 2.0},
        ],
    )

    class _IdleAdapter(_Adapter):
        async def working_order_ids(self):
            return {"someone-elses-order"}

    adapter = _IdleAdapter([{"symbol": "AAPL", "qty": 10.0}])

    _run_submit(adapter, deployment, _sell_trade_signal())

    assert len(adapter.submitted) == 1


def test_an_unverifiable_previous_exit_blocks(monkeypatch, caplog) -> None:
    """An adapter that cannot list its working orders cannot prove the
    previous exit is over: refuse rather than size off a stale ledger."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch,
        deployment,
        10.0,
        unfilled_exits=[
            {"signal_id": "sig-1", "broker_order_id": "ord-1", "requested_qty": 5.0,
             "filled_qty": 0.0},
        ],
    )
    adapter = _Adapter([{"symbol": "AAPL", "qty": 10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert adapter.submitted == []
    assert _records_at(caplog, logging.ERROR, "previous exit")


def test_no_unfilled_previous_exit_never_asks_the_broker(monkeypatch) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)

    class _MustNotAsk(_Adapter):
        async def working_order_ids(self):
            raise AssertionError("no previous exit is pending; do not ask the broker")

    adapter = _MustNotAsk([{"symbol": "AAPL", "qty": 10.0}])

    _run_submit(adapter, deployment, _sell_trade_signal())

    assert len(adapter.submitted) == 1


def _save_exit_signal(database, *, deployment_id, symbol, qty, filled, broker_order_id="ord"):
    signal = LiveSignal(
        id=uuid.uuid4(),
        deployment_id=deployment_id,
        symbol=symbol,
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "idempotency_key": uuid.uuid4().hex,
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
            "qty": qty,
            "explicit_qty": qty,
            "broker_order_id": broker_order_id,
        },
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    if filled:
        database.save_live_execution(
            LiveExecution(
                signal_id=uuid.UUID(signal_id),
                actual_price=100.0,
                filled_qty=filled,
                commission=0.0,
                timestamp=datetime.now(timezone.utc) - timedelta(seconds=1),
                broker_order_id=broker_order_id,
            )
        )
    return signal_id


def test_unfilled_exit_orders_lists_only_this_deployments_open_exits(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    seed_live_deployments(database, [mine, other])

    done = _save_exit_signal(
        database, deployment_id=mine, symbol="AAPL", qty=5.0, filled=5.0, broker_order_id="o1"
    )
    partial = _save_exit_signal(
        database, deployment_id=mine, symbol="AAPL", qty=5.0, filled=2.0, broker_order_id="o2"
    )
    unfilled = _save_exit_signal(
        database, deployment_id=mine, symbol="aapl", qty=3.0, filled=0.0, broker_order_id="o3"
    )
    _save_exit_signal(database, deployment_id=other, symbol="AAPL", qty=3.0, filled=0.0)
    _save_exit_signal(database, deployment_id=mine, symbol="MSFT", qty=3.0, filled=0.0)
    # An entry is not an exit, filled or not.
    _save_signal_with_fill(database, deployment_id=mine, symbol="AAPL", action="buy", qty=0.0)

    rows = database.list_live_deployment_unfilled_exit_orders(mine, "AAPL")

    by_id = {row["signal_id"]: row for row in rows}
    assert set(by_id) == {partial, unfilled}
    assert done not in by_id
    assert by_id[partial]["broker_order_id"] == "o2"
    assert by_id[partial]["requested_qty"] == pytest.approx(5.0)
    assert by_id[partial]["filled_qty"] == pytest.approx(2.0)
    assert by_id[unfilled]["filled_qty"] == 0.0


# ─── Review concern 4: nothing to exit while someone else holds the symbol ───


def test_nothing_to_exit_beside_a_broker_position_is_a_warning(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 0.0, others={_OTHER: 25.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 25.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    warnings = _records_at(caplog, logging.WARNING, "Exit signal ignored")
    assert warnings, [record.getMessage() for record in caplog.records]
    assert warnings[0].data["broker_qty"] == pytest.approx(25.0)


def test_nothing_to_exit_on_a_flat_book_stays_info(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 0.0)
    adapter = _Adapter([])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert _records_at(caplog, logging.INFO, "Exit signal ignored")
    assert not _records_at(caplog, logging.WARNING, "Exit signal ignored")


# ─── Review concern 6: an exit never rounds up to the instrument's step ──────
#
# cTrader converted an exit's qty to broker volume with round(), so a partial
# exit could close up to half a volume unit more than its fraction. Exits
# (partial and full) round DOWN to the instrument's step; one that rounds to
# nothing is ignored with a WARNING naming the requested qty and the step.


@pytest.mark.parametrize(
    ("qty", "step", "expected"),
    [
        (10 / 3, 1e-9, 3.333333333),
        (1 / 6, 1e-9, 0.166666666),
        (0.3336, 0.001, 0.333),
        (0.5, 0.01, 0.5),
        # Float noise of a whole quantity is not a lost unit.
        (2.9999999999999996, 1.0, 3.0),
        (0.30000000000000004, 0.1, 0.3),
        (0.125, 1.0, 0.0),
    ],
)
def test_floor_qty_to_step_to_step(qty, step, expected) -> None:
    from shared.lot_grid import floor_qty_to_step

    assert floor_qty_to_step(qty, step) == pytest.approx(expected, abs=1e-15)
    assert floor_qty_to_step(qty, step) <= qty + 1e-12


@pytest.mark.parametrize("step", [0.0, -1.0, float("nan"), float("inf")])
def test_floor_qty_to_step_refuses_an_unusable_step(step) -> None:
    from shared.lot_grid import floor_qty_to_step

    with pytest.raises(ValueError):
        floor_qty_to_step(1.0, step)


class _SteppedAdapter(_Adapter):
    """An adapter that names its exit quantity step."""

    def __init__(self, positions, step: float):
        super().__init__(positions)
        self.step = step

    def exit_qty_step(self, symbol: str) -> float:
        return self.step


def test_a_partial_exit_rounds_down_to_the_step(monkeypatch) -> None:
    """1/3 of 0.5 is 0.1666...; a 0.01 step closes 0.16, never 0.17."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 0.5)
    adapter = _SteppedAdapter([{"symbol": "AAPL", "qty": 0.5}], step=0.01)

    _run_submit(adapter, deployment, _exit_trade_signal(fraction=1 / 3))

    assert len(adapter.submitted) == 1
    signal, qty = adapter.submitted[0]
    assert qty == pytest.approx(0.16)
    # What is persisted is what is sent.
    assert signal.metadata["qty"] == pytest.approx(0.16)
    assert signal.metadata["explicit_qty"] == pytest.approx(0.16)


def test_a_full_exit_rounds_down_to_the_step(monkeypatch) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 0.567)
    adapter = _SteppedAdapter([{"symbol": "AAPL", "qty": 0.567}], step=0.01)

    _run_submit(adapter, deployment, _sell_trade_signal())

    assert len(adapter.submitted) == 1
    assert adapter.submitted[0][1] == pytest.approx(0.56)


def test_an_exit_that_rounds_to_nothing_is_ignored_with_a_warning(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 0.5)
    adapter = _SteppedAdapter([{"symbol": "AAPL", "qty": 0.5}], step=1.0)

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal(fraction=0.25))

    assert adapter.submitted == []
    warnings = _records_at(caplog, logging.WARNING, "below the instrument's quantity step")
    assert warnings, [record.getMessage() for record in caplog.records]
    assert warnings[0].data["requested_qty"] == pytest.approx(0.125)
    assert warnings[0].data["qty_step"] == pytest.approx(1.0)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


class _AdapterRefusingBelowStep(_Adapter):
    """The adapter's own floor (defence in depth) refuses before the request."""

    async def submit_order(self, signal, *, qty=None):
        from live_runner.base_adapter import order_not_submitted
        from live_runner.exit_signals import ExitBelowStepError

        self.submitted.append((signal, qty))
        with order_not_submitted():
            raise ExitBelowStepError(
                "exit rounds to nothing", requested_qty=0.0004, qty_step=0.001
            )


def test_an_adapter_below_step_refusal_is_a_warning_not_an_error(monkeypatch, caplog) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter = _AdapterRefusingBelowStep([{"symbol": "AAPL", "qty": 10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    warnings = _records_at(caplog, logging.WARNING, "below the instrument's quantity step")
    assert warnings, [record.getMessage() for record in caplog.records]
    assert warnings[0].data["requested_qty"] == pytest.approx(0.0004)
    assert warnings[0].data["qty_step"] == pytest.approx(0.001)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


# ─── Review (#142): exits are serialised per account and symbol ──────────────


class _WorkingOrdersAdapter(_Adapter):
    def __init__(self, positions, working):
        super().__init__(positions)
        self._working = set(working)

    async def working_order_ids(self):
        return set(self._working)


_OTHER_DEPLOYMENT_EXIT = {
    "signal_id": "sig-other",
    "deployment_id": _OTHER,
    "broker_order_id": "ord-other",
    "requested_qty": 5.0,
    "filled_qty": 0.0,
}


def test_an_exit_of_another_deployment_on_the_account_does_not_drop_this_exit(
    monkeypatch, caplog
) -> None:
    """Two deployments share one netted account and both hold AAPL: the first's
    exit is still working (market closed) when this one's is processed. This
    one is sized against what remains (broker 15 - 5 reserved), not dropped."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 5.0},
        other_unfilled_exits=[_OTHER_DEPLOYMENT_EXIT],
    )
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 15.0}], {"ord-other"})

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert [qty for _signal, qty in adapter.submitted] == [pytest.approx(10.0)]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_the_other_deployments_exit_is_found_by_its_client_order_id_too(monkeypatch) -> None:
    deployment = _deployment(allow_short=False)
    row = {**_OTHER_DEPLOYMENT_EXIT, "broker_order_id": ""}
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 5.0}, other_unfilled_exits=[row]
    )
    # Broker 12 and 5 reserved: only found via the client order id, the cap is 7.
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 12.0}], {"sig-other"})

    _run_submit(adapter, deployment, _exit_trade_signal())

    assert [qty for _signal, qty in adapter.submitted] == [pytest.approx(7.0)]


def test_only_the_unfilled_part_of_the_other_exit_is_reserved(monkeypatch) -> None:
    deployment = _deployment(allow_short=False)
    row = {**_OTHER_DEPLOYMENT_EXIT, "requested_qty": 5.0, "filled_qty": 3.0}
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 2.0}, other_unfilled_exits=[row]
    )
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 12.0}], {"ord-other"})

    _run_submit(adapter, deployment, _exit_trade_signal())

    assert [qty for _signal, qty in adapter.submitted] == [pytest.approx(10.0)]


def test_a_capped_exit_behind_another_working_exit_raises_the_ledger_alert(
    monkeypatch, caplog
) -> None:
    """Broker 12 - 5 reserved = 7 < own 10 (the ledgers add up to 15): the exit
    goes out capped at 7 and an operator is told."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 2.0},
        other_unfilled_exits=[_OTHER_DEPLOYMENT_EXIT],
    )
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 12.0}], {"ord-other"})

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert [qty for _signal, qty in adapter.submitted] == [pytest.approx(7.0)]
    alerts = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and getattr(record, "data", {}).get("alert") == "exit_ledger_mismatch"
    ]
    assert alerts, [record.getMessage() for record in caplog.records]


def test_nothing_left_after_the_other_working_exits_is_an_error_and_sends_nothing(
    monkeypatch, caplog
) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 5.0},
        other_unfilled_exits=[{**_OTHER_DEPLOYMENT_EXIT, "requested_qty": 15.0}],
    )
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 15.0}], {"ord-other"})

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert adapter.submitted == []
    errors = _records_at(caplog, logging.ERROR, "previous exit")
    assert errors, [record.getMessage() for record in caplog.records]
    assert "ord-other" in errors[0].data["reason"]
    assert _OTHER in errors[0].data["reason"]
    assert not _records_at(caplog, logging.WARNING, "previous exit")


def test_a_working_exit_of_the_other_deployments_opposite_side_reserves_nothing(
    monkeypatch,
) -> None:
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 0.0},
        other_unfilled_exits=[{**_OTHER_DEPLOYMENT_EXIT, "action": "buy"}],
    )
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 10.0}], {"ord-other"})

    _run_submit(adapter, deployment, _exit_trade_signal())

    assert [qty for _signal, qty in adapter.submitted] == [pytest.approx(10.0)]


def test_one_message_fans_out_to_two_deployments_and_both_exit(monkeypatch) -> None:
    """A then B on one account. A's market exit is submitted and stays working
    (no fills yet); B must still submit exactly its own quantity."""
    import live_runner.daemon as daemon

    first, second = _deployment(allow_short=False), _deployment(allow_short=False)
    _patch_ledger(monkeypatch, first, 6.0, others={str(second.id): 4.0})
    submitted_by_first: list[dict] = []

    def _account_unfilled(account_id, symbol, *, exclude_deployment_id=None):
        # A's exit, persisted at submission, is the only row; B sees it as other.
        if exclude_deployment_id == str(second.id):
            return []
        return [
            {"signal_id": "sig-a", "deployment_id": str(first.id),
             "broker_order_id": "ord-a", "requested_qty": 6.0, "filled_qty": 0.0,
             "action": "sell"}
        ] if submitted_by_first else []

    monkeypatch.setattr(daemon, "list_account_unfilled_exit_orders", _account_unfilled)

    class _Fan(_WorkingOrdersAdapter):
        async def submit_order(self, signal, *, qty=None):
            if not self.submitted:
                submitted_by_first.append({})
            return await super().submit_order(signal, qty=qty)

    adapter = _Fan([{"symbol": "AAPL", "qty": 10.0}], {"ord-a"})

    _run_submit(adapter, first, _exit_trade_signal())
    _run_submit(adapter, second, _exit_trade_signal())

    assert [qty for _signal, qty in adapter.submitted] == [
        pytest.approx(6.0),
        pytest.approx(4.0),
    ]


def test_once_the_other_deployments_exit_is_done_this_exit_is_capped_to_the_fresh_broker_qty(
    monkeypatch, caplog
) -> None:
    """The other exit is no longer working: this one goes ahead, sized against
    the broker position read now (its ledger says 10, the broker holds 4)."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(
        monkeypatch, deployment, 10.0, others={_OTHER: 0.0},
        other_unfilled_exits=[_OTHER_DEPLOYMENT_EXIT],
    )
    adapter = _WorkingOrdersAdapter([{"symbol": "AAPL", "qty": 4.0}], set())

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert len(adapter.submitted) == 1
    assert adapter.submitted[0][1] == pytest.approx(4.0)
    assert not _records_at(caplog, logging.WARNING, "previous exit")


def test_the_guard_asks_for_the_account_but_not_for_this_deployment(monkeypatch) -> None:
    import live_runner.daemon as daemon

    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0)
    seen: list[tuple] = []

    def _account_unfilled(account_id, symbol, **kwargs):
        seen.append((account_id, symbol, kwargs))
        return []

    monkeypatch.setattr(daemon, "list_account_unfilled_exit_orders", _account_unfilled)
    _run_submit(_Adapter([{"symbol": "AAPL", "qty": 10.0}]), deployment, _exit_trade_signal())

    assert seen == [
        (deployment.account_id, "AAPL", {"exclude_deployment_id": str(deployment.id)})
    ]


def test_concurrent_exits_of_two_deployments_are_sized_one_after_the_other(
    monkeypatch,
) -> None:
    """Both are dispatched at once. The second must not read the broker position
    until the first has submitted, and is then sized against the fresh position."""
    import live_runner.daemon as daemon

    first, second = _deployment(allow_short=False), _deployment(allow_short=False)
    _patch_ledger(monkeypatch, first, 10.0, others={str(second.id): 10.0})
    events: list[str] = []

    class _NettedAdapter(_Adapter):
        def __init__(self):
            super().__init__([{"symbol": "AAPL", "qty": 20.0}])
            self.release_first = asyncio.Event()

        async def get_positions(self):
            events.append("positions")
            return list(self.positions)

        async def submit_order(self, signal, *, qty=None):
            events.append(f"submit-start:{qty}")
            if len(self.submitted) == 0:
                self.submitted.append((signal, qty))
                await self.release_first.wait()
            else:
                self.submitted.append((signal, qty))
            self.positions = [{"symbol": "AAPL", "qty": 20.0 - sum(q for _, q in self.submitted)}]
            events.append("submit-end")
            return f"order-{len(self.submitted)}"

    adapter = _NettedAdapter()

    async def _no_wait(_signal, _timeframe):
        return None

    monkeypatch.setattr(daemon, "wait_for_candle_close", _no_wait)
    monkeypatch.setattr(daemon, "_deployment_can_dispatch", lambda _d, **_k: True)
    monkeypatch.setattr(daemon, "claim_live_deployment_submission", lambda *_a, **_k: True)
    monkeypatch.setattr(daemon, "release_live_deployment_submission", lambda *_a, **_k: True)

    async def _both() -> None:
        tasks = [
            asyncio.create_task(
                daemon._submit_normalized_discord_signal(
                    deployment.account_id,
                    adapter,
                    deployment,
                    _exit_trade_signal(),
                    signal_timeframe="1h",
                    wait_enabled=False,
                )
            )
            for deployment in (first, second)
        ]
        # The preparation runs partly in worker threads: wait for the first to
        # reach the broker, then give the second ample time to (wrongly) follow.
        for _ in range(500):
            if any(event.startswith("submit-start") for event in events):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        # The first is parked inside submit_order; the second has not even
        # looked at the broker position.
        assert events == ["positions", "submit-start:10.0"]
        adapter.release_first.set()
        await asyncio.gather(*tasks)

    asyncio.run(_both())

    assert events == [
        "positions", "submit-start:10.0", "submit-end",
        "positions", "submit-start:10.0", "submit-end",
    ]


def test_account_unfilled_exits_span_deployments_of_the_account_only(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine, sibling, stranger = (str(uuid.uuid4()) for _ in range(3))
    seed_live_deployments(database, [mine, sibling], account_id="shared-acc")
    seed_live_deployments(database, [stranger], account_id="other-acc")

    own = _save_exit_signal(database, deployment_id=mine, symbol="AAPL", qty=5.0, filled=0.0)
    siblings = _save_exit_signal(
        database, deployment_id=sibling, symbol="aapl", qty=5.0, filled=1.0, broker_order_id="o2"
    )
    _save_exit_signal(database, deployment_id=sibling, symbol="AAPL", qty=5.0, filled=5.0)
    _save_exit_signal(database, deployment_id=sibling, symbol="MSFT", qty=5.0, filled=0.0)
    _save_exit_signal(database, deployment_id=stranger, symbol="AAPL", qty=5.0, filled=0.0)

    everyone = database.list_account_unfilled_exit_orders("shared-acc", "AAPL")
    assert {row["signal_id"] for row in everyone} == {own, siblings}
    by_id = {row["signal_id"]: row for row in everyone}
    assert by_id[siblings]["deployment_id"] == sibling
    assert by_id[siblings]["broker_order_id"] == "o2"
    assert by_id[siblings]["filled_qty"] == pytest.approx(1.0)
    assert by_id[siblings]["action"] == "sell"

    others = database.list_account_unfilled_exit_orders(
        "shared-acc", "AAPL", exclude_deployment_id=mine
    )
    assert [row["signal_id"] for row in others] == [siblings]
    with pytest.raises(ValueError):
        database.list_account_unfilled_exit_orders("", "AAPL")


# ─── Review (#142): unreadable ledger actions fail closed ────────────────────


def test_ledgers_refuse_a_filled_signal_with_an_unknown_action(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    deployment_id = str(uuid.uuid4())
    seed_live_deployments(database, [deployment_id], account_id="acc-unknown")
    _save_signal_with_fill(
        database, deployment_id=deployment_id, symbol="AAPL", action="buy", qty=3.0
    )
    # The model's column is an unconstrained string, so a row can hold any action
    # (an older writer, a manual fix) even though LiveSignal itself cannot.
    with database.get_session() as session:
        for row in session.exec(database.select(database.LiveSignalModel)).all():
            row.action = "short"
            session.add(row)
        session.commit()

    with pytest.raises(ValueError, match="no known position sign"):
        database.get_live_deployment_net_filled_qty(deployment_id, "AAPL")
    with pytest.raises(ValueError, match="no known position sign"):
        database.get_account_net_filled_qty_by_deployment("acc-unknown", "AAPL")


# ─── Review (#142): the own-ledger-exceeds-broker alert, named directly ──────


def test_own_ledger_exceeds_broker_alerts_even_when_the_ledger_total_matches(
    monkeypatch, caplog
) -> None:
    """Own ledger +10 and another deployment's -4 add up to the broker's 6, so
    the total reconciles, yet this deployment's 10 is more than the broker holds:
    the exit is capped at 6 and an alert says the own ledger exceeds the broker."""
    deployment = _deployment(allow_short=False)
    _patch_ledger(monkeypatch, deployment, 10.0, others={_OTHER: -4.0})
    adapter = _Adapter([{"symbol": "AAPL", "qty": 6.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _sell_trade_signal())

    assert [qty for _, qty in adapter.submitted] == [6.0]
    alerts = _alerts(caplog)
    assert len(alerts) == 1, [record.getMessage() for record in caplog.records]
    assert alerts[0].data["own_ledger_qty"] == pytest.approx(10.0)
    assert alerts[0].data["broker_qty"] == pytest.approx(6.0)
    assert "own ledger quantity exceeds the broker position" in alerts[0].data["reason"]


def test_the_submitted_log_records_the_order_actually_sent(monkeypatch, caplog) -> None:
    """An exit of a short is a BUY at the broker whatever the parser's
    placeholder action says; the log must show the order, and that it is an exit."""
    deployment = _deployment(allow_short=True)
    _patch_ledger(monkeypatch, deployment, -10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": -10.0}])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal(side="short"))

    assert adapter.submitted[0][0].action == "buy"
    submitted = _records_at(caplog, logging.INFO, "Discord signal submitted")
    assert len(submitted) == 1
    assert submitted[0].data["action"] == "buy"
    assert submitted[0].data["exit"] is True


@pytest.mark.parametrize("exit_signal", [True, False], ids=["exit", "entry"])
def test_a_stale_exit_is_logged_at_warning_an_entry_at_info(caplog, exit_signal) -> None:
    from datetime import datetime, timedelta, timezone

    import live_runner.daemon as daemon

    signal = _exit_trade_signal() if exit_signal else _sell_trade_signal()
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    signal = dataclasses.replace(
        signal,
        action="sell" if exit_signal else "buy",
        metadata={**signal.metadata, "received_at": stale_at},
    )

    with caplog.at_level(logging.INFO):
        prepared = asyncio.run(
            daemon._prepare_discord_signal_for_submission(
                "acc", _deployment(allow_short=False), signal,
                signal_timeframe="1h", wait_enabled=False,
            )
        )

    assert prepared is None
    level = logging.WARNING if exit_signal else logging.INFO
    assert _records_at(caplog, level, "stale discord signal")
    assert not _records_at(caplog, logging.INFO if exit_signal else logging.WARNING, "stale")


def test_entries_are_skipped_while_the_reconciliation_halt_is_set(
    monkeypatch, caplog
) -> None:
    """#303: the entry-only halt blocks new entries but never exits."""
    import live_runner.daemon as daemon

    deployment = _deployment(allow_short=True)
    adapter = _Adapter([])
    monkeypatch.setattr(daemon, "live_deployment_entries_halted", lambda _id: True)
    entry = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        timeframe="1h",
        metadata=dict(_sell_trade_signal().metadata),
    )

    with caplog.at_level(logging.WARNING):
        _run_submit(adapter, deployment, entry)

    assert adapter.submitted == []
    assert any(
        "entries are halted by broker reconciliation" in record.getMessage()
        for record in caplog.records
    )

    # An exit on the same halted deployment still reaches the broker.
    _patch_ledger(monkeypatch, deployment, 10.0)
    adapter.positions = [{"symbol": "AAPL", "qty": 10.0}]
    _run_submit(adapter, deployment, _exit_trade_signal())
    assert len(adapter.submitted) == 1
