"""``live_executions.commission`` writes are guarded: a charge is a positive cost (#285).

The sign convention lived only in a docstring; now ``LiveExecution`` and
``LiveExecutionModel`` refuse a negative or non-finite commission, so no writer path
(Alpaca fills and CFEE backfill, cTrader stream and reconcile fills, the cumulative
amendment path) can store the wrong sign.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from shared.database import LiveExecutionModel
from shared.models import LiveExecution, LiveSignal, validate_commission_cost
from tests.conftest import seed_live_deployments

T0 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
DEP_1 = "11111111-1111-4111-8111-aaaaaaaaaaaa"


def _execution(commission: float | None) -> LiveExecution:
    return LiveExecution(
        signal_id=uuid4(),
        actual_price=100.0,
        filled_qty=5.0,
        commission=commission,
        timestamp=T0,
        broker_order_id="order-1",
    )


@pytest.mark.parametrize("bad", [-0.01, -1.0, math.nan, math.inf, -math.inf, True, "1.5"])
def test_live_execution_refuses_an_unusable_commission(bad: float) -> None:
    """`mode="before"` sees the raw value: `True` and `"1.5"` must not be coerced first."""
    with pytest.raises(ValidationError, match="commission"):
        _execution(bad)


@pytest.mark.parametrize("good", [None, 0.0, -0.0, 0.02, 3.5])
def test_live_execution_accepts_a_storable_commission(good: float | None) -> None:
    assert _execution(good).commission == (0.0 if good == 0 else good)


@pytest.mark.parametrize("bad", [-0.01, math.nan, math.inf])
def test_from_live_execution_refuses_an_unusable_commission(bad: float) -> None:
    """The persistence funnel guards even a ``LiveExecution`` that skipped validation."""
    execution = LiveExecution.model_construct(
        id=uuid4(),
        signal_id=uuid4(),
        actual_price=100.0,
        filled_qty=5.0,
        commission=bad,
        timestamp=T0,
        broker_order_id="order-1",
    )
    with pytest.raises(ValueError, match="commission"):
        LiveExecutionModel.from_live_execution(execution)


def test_the_write_guard_itself() -> None:
    assert validate_commission_cost(None) is None
    assert validate_commission_cost(0.0) == 0.0
    assert validate_commission_cost(2.5) == 2.5
    with pytest.raises(ValueError, match="non-negative"):
        validate_commission_cost(-1.0)
    with pytest.raises(ValueError, match="finite"):
        validate_commission_cost(math.nan)
    with pytest.raises(ValueError, match="finite"):
        validate_commission_cost(True)
    with pytest.raises(ValueError, match="finite"):
        validate_commission_cost("1.5")
    with pytest.raises(ValueError, match="finite"):
        validate_commission_cost(10**10000)  # huge int: isfinite would OverflowError


@pytest.fixture
def database(isolated_db):
    db = isolated_db[0]
    seed_live_deployments(db, [DEP_1])
    return db


def _persist_signal(database) -> str:
    signal = LiveSignal(
        deployment_id=DEP_1,
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.2345,
        timestamp=T0,
        metadata={},
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    return signal_id


def _tampered_execution(signal_id: str, commission: float) -> LiveExecution:
    """A ``LiveExecution`` that bypassed its own validator, as a buggy adapter could."""
    return LiveExecution.model_construct(
        id=uuid4(),
        signal_id=UUID(signal_id),
        actual_price=100.0,
        filled_qty=5.0,
        commission=commission,
        timestamp=T0,
        broker_order_id="order-1",
        dedupe_key=None,
        broker_fill_id=None,
        actual_price_observed=None,
    )


def test_save_live_execution_refuses_a_negative_commission(database) -> None:
    """The plain persist path every adapter funnels through (Alpaca and cTrader)."""
    signal_id = _persist_signal(database)
    with pytest.raises(ValueError, match="commission"):
        database.save_live_execution(_tampered_execution(signal_id, -1.0))


def test_save_live_execution_up_to_cumulative_refuses_a_negative_commission(database) -> None:
    """The reconciliation/amendment path shares the same model guard."""
    signal_id = _persist_signal(database)
    with pytest.raises(ValueError, match="commission"):
        database.save_live_execution_up_to_cumulative(_tampered_execution(signal_id, -1.0), 5.0)


def test_ctrader_rebate_is_stored_as_unknown_fee(caplog) -> None:
    """A cTrader rebate converts to a negative cost; ``_deal_commission`` maps it to
    ``None`` ("fee unknown") and logs ERROR with the deal id instead of letting the
    write guard refuse the whole fill (#285)."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOADeal

    from live_runner.ctrader_adapter import _deal_commission

    deal = ProtoOADeal(dealId=42, commission=1234, moneyDigits=3)  # +1.234 cash flow: a rebate
    with caplog.at_level("ERROR"):
        assert _deal_commission(deal) is None
    assert any("rebate" in r.message and "42" in r.message for r in caplog.records)
    # The fill itself still validates and stores, with commission NULL.
    assert _execution(None).commission is None


def test_ctrader_charge_still_passes_the_guard() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOADeal

    from live_runner.ctrader_adapter import _deal_commission

    deal = ProtoOADeal(commission=-350, moneyDigits=2)  # -3.50 cash flow: a charge
    assert _execution(_deal_commission(deal)).commission == pytest.approx(3.5)


def test_alpaca_commission_values_pass_the_guard() -> None:
    """Alpaca only ever writes ``0.0`` (known zero) or ``None`` (unknown until CFEE)."""
    from live_runner.alpaca_adapter import _known_commission

    equity = SimpleNamespace(asset_class="us_equity")
    crypto = SimpleNamespace(asset_class="crypto")
    assert _known_commission(equity) == 0.0
    assert _known_commission(crypto) is None
    assert _execution(_known_commission(equity)).commission == 0.0
    assert _execution(_known_commission(crypto)).commission is None
