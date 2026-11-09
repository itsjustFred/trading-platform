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
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

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

    async def submit_order(self, signal, *, qty=None):
        self.submitted.append((signal, qty))
        return "order-121"


def _patch_ledger(monkeypatch, qty: float) -> list[tuple[str, str]]:
    import live_runner.daemon as daemon

    calls: list[tuple[str, str]] = []

    def _ledger(deployment_id: str, symbol: str) -> float:
        calls.append((deployment_id, symbol))
        return qty

    monkeypatch.setattr(daemon, "get_live_deployment_net_filled_qty", _ledger)
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
    calls = _patch_ledger(monkeypatch, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 25.0}])
    live = _trade_signal_to_live_signal(_exit_trade_signal(), deployment)

    prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert calls == [(str(deployment.id), "AAPL")]
    assert qty == pytest.approx(10.0)
    assert prepared.action == "sell"
    assert prepared.metadata["qty"] == pytest.approx(10.0)
    assert prepared.metadata["explicit_qty"] == pytest.approx(10.0)
    assert prepared.metadata["risk_sizing_mode"] == "exit_only"
    # No entry levels: the adapter builds a plain close, not a bracket.
    assert "stop_loss" not in prepared.metadata
    assert "take_profit" not in prepared.metadata


def test_prepare_takes_the_close_side_from_the_position(monkeypatch) -> None:
    from live_runner.daemon import _trade_signal_to_live_signal, prepare_order_with_risk

    deployment = _deployment(allow_short=True)
    _patch_ledger(monkeypatch, -4.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": -4.0}])
    live = _trade_signal_to_live_signal(_exit_trade_signal(side=None), deployment)

    prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert prepared.action == "buy"
    assert qty == pytest.approx(4.0)


def test_prepare_partial_exit(monkeypatch) -> None:
    from live_runner.daemon import _trade_signal_to_live_signal, prepare_order_with_risk

    deployment = _deployment()
    _patch_ledger(monkeypatch, 10.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 10.0}])
    live = _trade_signal_to_live_signal(_exit_trade_signal(fraction=0.5), deployment)

    _prepared, qty = asyncio.run(prepare_order_with_risk(adapter, deployment, live))

    assert qty == pytest.approx(5.0)


def _run_submit(adapter, deployment, trade_signal) -> None:
    import live_runner.daemon as daemon

    async def _no_wait(_signal, _timeframe):
        return None

    original_wait = daemon.wait_for_candle_close
    original_active = daemon._deployment_is_currently_active
    original_claim = daemon.claim_live_deployment_submission
    original_release = daemon.release_live_deployment_submission
    daemon.wait_for_candle_close = _no_wait
    daemon._deployment_is_currently_active = lambda _deployment: True
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
        daemon._deployment_is_currently_active = original_active
        daemon.claim_live_deployment_submission = original_claim
        daemon.release_live_deployment_submission = original_release


def test_exit_without_a_position_is_ignored_and_logged(monkeypatch, caplog) -> None:
    deployment = _deployment()
    _patch_ledger(monkeypatch, 0.0)
    adapter = _Adapter([])

    with caplog.at_level(logging.INFO):
        _run_submit(adapter, deployment, _exit_trade_signal())

    assert adapter.submitted == []
    assert any("Exit signal ignored" in record.getMessage() for record in caplog.records)


def test_exit_with_a_position_is_submitted_as_exit_only(monkeypatch) -> None:
    deployment = _deployment()
    _patch_ledger(monkeypatch, 10.0)
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
    _patch_ledger(monkeypatch, 10.0)
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
    _patch_ledger(monkeypatch, 3.0)
    adapter = _Adapter([{"symbol": "AAPL", "qty": 3.0}])

    _run_submit(adapter, deployment, trade_signal)

    assert len(adapter.submitted) == 1
    signal, qty = adapter.submitted[0]
    assert signal.action == "sell", "an exit of a long must sell, never buy"
    assert qty == pytest.approx(3.0)
