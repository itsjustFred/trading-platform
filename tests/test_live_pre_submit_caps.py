"""Pre-submit hard caps (#302): the last check before a broker submit.

``live_runner.pre_submit_caps.enforce_pre_submit_caps`` runs after sizing and
the submission claim, immediately before ``adapter.submit_order``, on both
submit chokepoints (``submit_order_with_risk`` and the discord dispatch in
``_prepare_claim_and_submit_signal``). It is independent of the sizing code:
every figure is re-derived from the broker (quote, pip value, balance) and
the deployment's trusted ``settings``.

It refuses — fail closed, ERROR alert + audit record — when qty or notional
breaks a configured cap, the entry deviates from the live quote by more than
the configured fraction, or the risk at the stop exceeds a configured
multiple of the deployment's configured risk. Exit-only orders are never
checked: they reduce exposure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
from live_runner.base_adapter import (
    BaseLiveAdapter,
    LivePipValue,
    LiveQuote,
    is_order_not_submitted,
)
from live_runner.pre_submit_caps import (
    PreSubmitCapViolation,
    enforce_pre_submit_caps,
    pre_submit_cap_settings,
    validate_pre_submit_cap_settings,
)
from risk.config import RiskConfig
from shared.database import list_live_control_audit
from shared.models import LiveDeploymentConfig, LiveSignal


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=uuid4(),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings=settings,
    )


def _signal(
    deployment: LiveDeploymentConfig,
    *,
    symbol: str = "AAPL",
    action: str = "buy",
    metadata: dict[str, Any] | None = None,
) -> LiveSignal:
    meta = {"entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0}
    meta.update(metadata or {})
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=meta,
    )


@dataclass
class _Broker(BaseLiveAdapter):
    """Adapter double with broker truth the caps re-derive independently."""

    # The promotion gate (#165) treats an adapter that does not prove it is
    # demo/paper as real money; the double says what it is.
    demo: bool = True
    quote: LiveQuote = field(
        default_factory=lambda: LiveQuote(
            bid=100.0, ask=100.0, timestamp=datetime.now(timezone.utc), source="test"
        )
    )
    pip_value: float = 1.0
    equity: float = 100_000.0
    max_leverage: float | None = None
    qty_step: float | None = None
    quote_error: Exception | None = None
    pip_error: Exception | None = None
    balance_error: Exception | None = None
    leverage_error: Exception | None = None
    submitted: list[tuple[LiveSignal, float | None]] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal, *, qty: float | None = None) -> str:
        self.submitted.append((signal, qty))
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_quote(self, symbol: str) -> LiveQuote:
        if self.quote_error is not None:
            raise self.quote_error
        return self.quote

    async def get_pip_value(self, symbol: str) -> LivePipValue:
        if self.pip_error is not None:
            raise self.pip_error
        return LivePipValue(
            value=self.pip_value,
            units_per_qty=1.0,
            conversion_rate=1.0,
            quote_asset="USD",
            account_asset="USD",
            source="test",
        )

    async def get_balance(self) -> tuple[float, float]:
        if self.balance_error is not None:
            raise self.balance_error
        return self.equity, self.equity

    async def get_max_leverage(self, symbol: str) -> float | None:
        if self.leverage_error is not None:
            raise self.leverage_error
        return self.max_leverage

    def exit_qty_step(self, symbol: str) -> float:
        # Both real adapters name a step; 1e-9 is Alpaca's, effectively no floor.
        return self.qty_step if self.qty_step is not None else 1e-9

    async def get_positions(self) -> list[dict[str, Any]]:
        return []


def _risk_config() -> RiskConfig:
    return RiskConfig(max_risk_per_trade=0.01)


def _run(coro):
    return asyncio.run(coro)


# ── settings resolution ───────────────────────────────────────────────────────


def test_cap_settings_defaults_are_the_accepted_backstops():
    caps = pre_submit_cap_settings({}, symbol="AAPL")
    assert caps.max_qty is None
    # No fixed equity-fraction default: the notional ceiling is the broker's
    # maximum leverage (#240), failing closed to 1x when unknown.
    assert caps.max_notional_equity_fraction is None
    assert caps.max_price_deviation_fraction == pytest.approx(0.02)
    assert caps.max_risk_multiple == pytest.approx(2.0)


def test_cap_settings_per_symbol_override_wins_for_that_symbol_only():
    settings = {
        "pre_submit_max_notional_equity_fraction": 0.1,
        "pre_submit_symbol_caps": {
            "aapl": {"pre_submit_max_notional_equity_fraction": 0.5},
        },
    }
    assert (
        pre_submit_cap_settings(settings, symbol="AAPL").max_notional_equity_fraction
        == pytest.approx(0.5)
    )
    assert (
        pre_submit_cap_settings(settings, symbol="MSFT").max_notional_equity_fraction
        == pytest.approx(0.1)
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"pre_submit_max_risk_multiple": "abc"},
        {"pre_submit_max_price_deviation_fraction": -0.5},
        {"pre_submit_max_qty": 0},
        {"pre_submit_symbol_caps": {"AAPL": {"pre_submit_typo": 1.0}}},
        {"pre_submit_symbol_caps": "nope"},
        {"pre_submit_max_qtyy": 5.0},
    ],
)
def test_cap_settings_reject_invalid_config(settings):
    with pytest.raises(ValueError):
        pre_submit_cap_settings(settings, symbol="AAPL")


def test_validate_cap_settings_accepts_deployment_settings_with_other_keys():
    # Deployment settings hold every policy key; only cap-prefixed names are policed.
    validate_pre_submit_cap_settings(
        {"max_risk_per_trade": 0.01, "pre_submit_max_qty": 10.0}
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"pre_submit_max_qty": 0},
        {"pre_submit_max_qty": -3.0},
        {"pre_submit_max_notional_equity_fraction": 0},
        {"pre_submit_max_price_deviation_fraction": -0.1},
        {"pre_submit_max_risk_multiple": 0},
        {"pre_submit_symbol_caps": {"AAPL": {"pre_submit_max_qty": 0}}},
        {"pre_submit_symbol_caps": {"AAPL": {"pre_submit_max_risk_multiple": -1.0}}},
    ],
)
def test_validate_cap_settings_rejects_nonpositive_values_at_creation(settings):
    # A nonpositive cap stored at creation would refuse every entry of the
    # deployment at the first signal; it must be refused when written.
    with pytest.raises(ValueError):
        validate_pre_submit_cap_settings(settings)


def test_validate_cap_settings_rejects_case_duplicate_symbol_overrides():
    # "AAPL" and "aapl" are the same symbol key; resolving by insertion order
    # would make the effective cap depend on dict ordering.
    with pytest.raises(ValueError):
        validate_pre_submit_cap_settings(
            {
                "pre_submit_symbol_caps": {
                    "AAPL": {"pre_submit_max_qty": 1.0},
                    "aapl": {"pre_submit_max_qty": 2.0},
                }
            }
        )


# ── the checks ────────────────────────────────────────────────────────────────


def test_entry_within_caps_passes():
    deployment = _deployment()
    broker = _Broker()
    _run(
        enforce_pre_submit_caps(
            broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
        )
    )


def test_qty_cap_refuses():
    deployment = _deployment(pre_submit_max_qty=100.0)
    broker = _Broker()
    with pytest.raises(PreSubmitCapViolation, match="qty_cap"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_notional_cap_refuses():
    # 200 * 100 * 1.0 = 20_000 notional = 0.2 of the 100_000 equity.
    deployment = _deployment(pre_submit_max_notional_equity_fraction=0.1)
    broker = _Broker()
    with pytest.raises(PreSubmitCapViolation, match="notional_cap"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_notional_uses_pip_value_for_conversion():
    # A pip value of 10 makes the same order 10x the notional.
    deployment = _deployment(pre_submit_max_notional_equity_fraction=0.5)
    broker = _Broker(pip_value=10.0)
    with pytest.raises(PreSubmitCapViolation, match="notional_cap"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


@pytest.mark.parametrize("ask", [103.0, 97.0])
def test_price_deviation_refuses_in_either_direction(ask):
    deployment = _deployment()
    broker = _Broker(
        quote=LiveQuote(bid=ask, ask=ask, timestamp=datetime.now(timezone.utc), source="test")
    )
    with pytest.raises(PreSubmitCapViolation, match="price_deviation"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_price_deviation_uses_the_fill_side():
    # A sell fills against the bid, not the ask.
    deployment = _deployment()
    broker = _Broker(
        quote=LiveQuote(bid=97.0, ask=100.0, timestamp=datetime.now(timezone.utc), source="test")
    )
    with pytest.raises(PreSubmitCapViolation, match="price_deviation"):
        _run(
            enforce_pre_submit_caps(
                broker,
                deployment,
                _signal(
                    deployment,
                    action="sell",
                    metadata={"stop_loss": 105.0, "take_profit": 90.0},
                ),
                qty=200.0,
                risk_config=_risk_config(),
            )
        )


def test_stop_risk_multiple_refuses():
    # 500 * |100 - 95| * 1.0 = 2500 at the stop; configured risk is 0.01 *
    # 100_000 = 1000, so the 2.0x cap is 2000.
    deployment = _deployment()
    broker = _Broker()
    with pytest.raises(PreSubmitCapViolation, match="stop_risk"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=500.0, risk_config=_risk_config()
            )
        )


def test_default_caps_clamp_a_runaway_size_then_refuse_on_risk():
    # No settings at all, unknown broker leverage (1x): an order worth 2x
    # equity is clamped to the 1x ceiling (qty 2000 -> 1000), and the clamped
    # size still breaks the 2.0x stop-risk cap, so it is refused.
    deployment = _deployment()
    broker = _Broker()
    with pytest.raises(PreSubmitCapViolation, match="stop_risk"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=2000.0, risk_config=_risk_config()
            )
        )


def test_per_symbol_cap_applies_to_the_matching_symbol():
    deployment = _deployment(
        pre_submit_max_notional_equity_fraction=0.1,
        pre_submit_symbol_caps={"AAPL": {"pre_submit_max_notional_equity_fraction": 0.5}},
    )
    broker = _Broker()
    # AAPL override (0.5) admits the 0.2-equity order.
    _run(
        enforce_pre_submit_caps(
            broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
        )
    )
    # A symbol without an override keeps the deployment-wide 0.1 cap.
    with pytest.raises(PreSubmitCapViolation, match="notional_cap"):
        _run(
            enforce_pre_submit_caps(
                broker,
                deployment,
                _signal(deployment, symbol="MSFT"),
                qty=200.0,
                risk_config=_risk_config(),
            )
        )


# ── broker-leverage ceiling: clamp, never refuse (#240) ───────────────────────


def _loose_risk_config() -> RiskConfig:
    return RiskConfig(max_risk_per_trade=0.5)


def test_broker_leverage_clamps_instead_of_refusing():
    # 247x needed: qty 247_000 * 100 * 1.0 = 24.7M notional on 100k equity.
    # The broker allows 200x -> the qty clamps to the 200x ceiling (200_000),
    # floored to the step, and the order may submit at the clamped size.
    deployment = _deployment()
    broker = _Broker(max_leverage=200.0)
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    qty = _run(
        enforce_pre_submit_caps(
            broker,
            deployment,
            signal,
            qty=247_000.0,
            risk_config=_loose_risk_config(),
        )
    )
    assert qty == pytest.approx(200_000.0)


def test_leverage_clamp_floors_to_the_quantity_step():
    deployment = _deployment()
    broker = _Broker(max_leverage=200.0, qty_step=3.0)
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    qty = _run(
        enforce_pre_submit_caps(
            broker,
            deployment,
            signal,
            qty=247_000.0,
            risk_config=_loose_risk_config(),
        )
    )
    # 200_000 floored to whole 3.0 steps is 199_998 — never rounded up.
    assert qty == pytest.approx(199_998.0)


def test_unknown_broker_leverage_fails_closed_to_1x():
    # No broker maximum -> the ceiling is 1x equity: qty 2000 (2x) clamps to
    # the 1x max of 1000 instead of being refused.
    deployment = _deployment()
    broker = _Broker()
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    qty = _run(
        enforce_pre_submit_caps(
            broker,
            deployment,
            signal,
            qty=2000.0,
            risk_config=_loose_risk_config(),
        )
    )
    assert qty == pytest.approx(1000.0)


def test_leverage_lookup_failure_fails_closed_to_1x():
    deployment = _deployment()
    broker = _Broker(leverage_error=RuntimeError("leverage endpoint down"))
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    qty = _run(
        enforce_pre_submit_caps(
            broker,
            deployment,
            signal,
            qty=2000.0,
            risk_config=_loose_risk_config(),
        )
    )
    assert qty == pytest.approx(1000.0)


def test_garbage_leverage_read_clamps_to_1x():
    # A present-but-unparseable broker leverage (Alpaca's ``multiplier`` now
    # parses through ``parse_optional`` and raises ``MoneyParseError``) must
    # degrade to the 1x ceiling, never be swallowed into a guessed leverage.
    from shared.money import MoneyParseError

    deployment = _deployment()
    broker = _Broker(leverage_error=MoneyParseError("unreadable money value: 'junk'"))
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    qty = _run(
        enforce_pre_submit_caps(
            broker,
            deployment,
            signal,
            qty=2000.0,
            risk_config=_loose_risk_config(),
        )
    )
    assert qty == pytest.approx(1000.0)


def test_leverage_clamp_below_one_step_refuses():
    # The broker maximum allows less than one quantity step: no compliant
    # order exists, so this is a refusal, not a clamp to zero.
    deployment = _deployment()
    broker = _Broker(max_leverage=200.0, qty_step=1e9)
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    with pytest.raises(PreSubmitCapViolation, match="broker_leverage_cap"):
        _run(
            enforce_pre_submit_caps(
                broker,
                deployment,
                signal,
                qty=247_000.0,
                risk_config=_loose_risk_config(),
            )
        )


def test_configured_notional_fraction_still_refuses_below_broker_max():
    # An explicit operator cap is a hard refuse cap even when the broker
    # maximum would admit the order.
    deployment = _deployment(pre_submit_max_notional_equity_fraction=0.1)
    broker = _Broker(max_leverage=200.0)
    with pytest.raises(PreSubmitCapViolation, match="notional_cap"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_leverage_clamp_writes_an_audit_record(caplog):
    import logging

    deployment = _deployment()
    broker = _Broker(max_leverage=200.0)
    signal = _signal(deployment, metadata={"stop_loss": 99.9, "take_profit": 105.0})
    with caplog.at_level(logging.WARNING):
        qty = _run(
            enforce_pre_submit_caps(
                broker,
                deployment,
                signal,
                qty=247_000.0,
                risk_config=_loose_risk_config(),
            )
        )
    assert qty == pytest.approx(200_000.0)
    assert "clamped the order qty" in caplog.text
    records = [
        row
        for row in list_live_control_audit(limit=50)
        if row["action"] == "pre_submit_leverage_clamp"
        and row["target_id"] == str(deployment.id)
    ]
    assert records, "the clamp must leave an audit record"
    assert records[0]["succeeded"]
    assert records[0]["detail"]["qty_before_clamp"] == pytest.approx(247_000.0)
    assert records[0]["detail"]["qty_after_clamp"] == pytest.approx(200_000.0)


# ── fail closed ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "broker",
    [
        _Broker(quote_error=RuntimeError("no quote")),
        _Broker(pip_error=RuntimeError("no pip value")),
        _Broker(balance_error=RuntimeError("no balance")),
    ],
)
def test_unverifiable_broker_facts_refuse(broker):
    deployment = _deployment()
    with pytest.raises(PreSubmitCapViolation, match="broker_facts_unavailable"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_unusable_quote_refuses():
    deployment = _deployment()
    broker = _Broker(quote=LiveQuote(bid=105.0, ask=100.0))  # crossed
    with pytest.raises(PreSubmitCapViolation, match="broker_facts_unusable"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_stale_quote_refuses():
    # The caps compare against a live quote: a cached spot older than the
    # deployment's max_entry_quote_age_seconds must not pass.
    deployment = _deployment()
    broker = _Broker(
        quote=LiveQuote(
            bid=100.0,
            ask=100.0,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=3600),
            source="test",
        )
    )
    with pytest.raises(PreSubmitCapViolation, match="stale_quote"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_quote_without_a_timestamp_refuses():
    deployment = _deployment()
    broker = _Broker(quote=LiveQuote(bid=100.0, ask=100.0, timestamp=None, source="test"))
    with pytest.raises(PreSubmitCapViolation, match="stale_quote"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


def test_missing_stop_refuses():
    deployment = _deployment()
    broker = _Broker()
    signal = _signal(deployment, metadata={"stop_loss": None, "take_profit": 110.0})
    with pytest.raises(PreSubmitCapViolation, match="missing_stop_loss"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, signal, qty=200.0, risk_config=_risk_config()
            )
        )


def test_invalid_cap_config_refuses():
    deployment = _deployment(pre_submit_max_risk_multiple="lots")
    broker = _Broker()
    with pytest.raises(PreSubmitCapViolation, match="invalid_cap_config"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, _signal(deployment), qty=200.0, risk_config=_risk_config()
            )
        )


# ── exits are never capped ────────────────────────────────────────────────────


def test_exit_only_order_is_never_checked():
    deployment = _deployment(pre_submit_max_qty=1.0)

    class _ExplodingBroker(_Broker):
        async def get_quote(self, symbol):  # pragma: no cover - must not run
            raise AssertionError("caps must not read broker facts for an exit")

    broker = _ExplodingBroker()
    signal = _signal(
        deployment,
        action="sell",
        metadata={
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
            "stop_loss": None,
            "take_profit": None,
        },
    )
    # qty far above every cap and no levels at all: still passes untouched.
    _run(
        enforce_pre_submit_caps(
            broker, deployment, signal, qty=10**9, risk_config=_risk_config()
        )
    )


def test_entry_carrying_exit_markers_is_still_capped():
    """The exit bypass is the daemon's exit classification, not a payload
    marker: an entry that claims ``risk_sizing_mode=exit_only`` is sized as an
    entry and must still be capped."""
    deployment = _deployment(pre_submit_max_qty=1.0)
    broker = _Broker()
    signal = _signal(
        deployment,
        metadata={"exit_only": True, "risk_sizing_mode": "exit_only"},
    )
    with pytest.raises(PreSubmitCapViolation, match="qty_cap"):
        _run(
            enforce_pre_submit_caps(
                broker, deployment, signal, qty=200.0, risk_config=_risk_config()
            )
        )


# ── alert + audit record ──────────────────────────────────────────────────────


def test_refusal_writes_an_error_alert_and_an_audit_record(caplog):
    import logging

    deployment = _deployment(pre_submit_max_qty=100.0)
    broker = _Broker()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(PreSubmitCapViolation):
            _run(
                enforce_pre_submit_caps(
                    broker,
                    deployment,
                    _signal(deployment),
                    qty=200.0,
                    risk_config=_risk_config(),
                )
            )
    assert "pre-submit hard cap refused a live order (qty_cap)" in caplog.text
    records = [
        row
        for row in list_live_control_audit(limit=50)
        if row["action"] == "pre_submit_cap_refusal"
        and row["target_id"] == str(deployment.id)
    ]
    assert records, "the refusal must leave an audit record"
    assert records[0]["detail"]["rule"] == "qty_cap"
    assert records[0]["detail"]["qty"] == 200.0


# ── wiring: both submit chokepoints enforce the caps ─────────────────────────


def test_submit_order_with_risk_refuses_before_the_broker_call():
    deployment = _deployment(pre_submit_max_notional_equity_fraction=0.01)
    broker = _Broker()
    with pytest.raises(PreSubmitCapViolation):
        _run(daemon.submit_order_with_risk(broker, deployment, _signal(deployment)))
    assert broker.submitted == []


def test_submit_order_with_risk_submits_a_normal_order():
    deployment = _deployment()
    broker = _Broker()
    order_id = _run(daemon.submit_order_with_risk(broker, deployment, _signal(deployment)))
    assert order_id == "order-1"
    assert len(broker.submitted) == 1


def test_cap_refusal_is_marked_pre_request():
    deployment = _deployment(pre_submit_max_qty=1.0)
    broker = _Broker()
    try:
        _run(daemon.submit_order_with_risk(broker, deployment, _signal(deployment)))
    except PreSubmitCapViolation as exc:
        assert is_order_not_submitted(exc)
    else:  # pragma: no cover
        pytest.fail("the cap must refuse this order")
    assert broker.submitted == []


def test_entry_path_strips_exit_markers_before_the_adapter():
    """An entry carrying ``exit_only``/``risk_sizing_mode`` payload markers is
    sized as an entry; the markers must not survive to the adapter, which keys
    on them to build a reduce-only order without SL/TP legs."""
    deployment = _deployment()
    broker = _Broker()
    signal = _signal(
        deployment,
        metadata={"exit_only": True, "risk_sizing_mode": "exit_only"},
    )
    _run(daemon.submit_order_with_risk(broker, deployment, signal))
    assert len(broker.submitted) == 1
    submitted_metadata = broker.submitted[0][0].metadata or {}
    assert "exit_only" not in submitted_metadata
    assert "risk_sizing_mode" not in submitted_metadata


def test_claim_path_cap_refusal_releases_the_lease_and_never_submits(monkeypatch, caplog):
    """The production chokepoint: a cap refusal inside
    ``_prepare_claim_and_submit_signal`` must leave ``submit_order`` uncalled,
    release the submission lease and log a pre-request refusal."""
    import logging

    from risk import TradeSignal

    deployment = _deployment(pre_submit_max_qty=1.0)
    broker = _Broker()
    trade_signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        timeframe="1h",
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
    )
    live_signal = daemon._trade_signal_to_live_signal(trade_signal, deployment)

    released: list[str] = []
    monkeypatch.setattr(daemon, "claim_live_deployment_submission", lambda *a, **k: True)
    monkeypatch.setattr(
        daemon,
        "release_live_deployment_submission",
        lambda token: released.append(token) or True,
    )
    monkeypatch.setattr(daemon, "renew_live_deployment_submission", lambda token: True)

    with caplog.at_level(logging.WARNING):
        _run(
            daemon._prepare_claim_and_submit_signal(
                "acc-1",
                broker,
                deployment,
                trade_signal,
                live_signal,
                is_exit=False,
            )
        )

    assert broker.submitted == []
    assert released, "a provably pre-request refusal must release the submission lease"
    assert "released the submission lease" in caplog.text


def test_claim_path_submits_the_clamped_qty(monkeypatch):
    """The Discord claim path must submit the quantity the caps returned.

    Pins ``prepared_qty = await enforce_pre_submit_caps(...)`` at the claim
    chokepoint: a mutant that drops the assignment would submit the pre-clamp
    size on the main live path.
    """
    from risk import TradeSignal

    deployment = _deployment(max_risk_per_trade=0.5)
    broker = _Broker(max_leverage=2.0)
    trade_signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        timeframe="1h",
        metadata={"received_at": datetime.now(timezone.utc).isoformat()},
    )
    live_signal = daemon._trade_signal_to_live_signal(trade_signal, deployment)
    prepared_signal = _signal(
        deployment,
        metadata={"entry_price": 100.0, "stop_loss": 99.9, "take_profit": 105.0},
    )

    async def _fake_prepare(adapter, dep, signal, **_kwargs):
        # A runaway size: 5000 * 100 * 1.0 = 500k notional = 5x the 100k
        # equity; the 2x broker maximum clamps it to 2000.
        return prepared_signal, 5000.0

    monkeypatch.setattr(daemon, "prepare_order_with_risk", _fake_prepare)
    monkeypatch.setattr(daemon, "claim_live_deployment_submission", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "release_live_deployment_submission", lambda token: True)
    monkeypatch.setattr(daemon, "renew_live_deployment_submission", lambda token: True)

    _run(
        daemon._prepare_claim_and_submit_signal(
            "acc-1",
            broker,
            deployment,
            trade_signal,
            live_signal,
            is_exit=False,
        )
    )

    assert len(broker.submitted) == 1
    _submitted_signal, submitted_qty = broker.submitted[0]
    assert submitted_qty == pytest.approx(2000.0)
    assert submitted_qty != pytest.approx(5000.0)


# ── creation-time validation (#302) ──────────────────────────────────────────


def _seed_account(database, account_id: str) -> None:
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id=account_id, broker="alpaca", name="paper", is_demo=True
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "settings",
    [
        {"pre_submit_max_qty": 0},
        {"pre_submit_max_qty": -5},
        {"pre_submit_symbol_caps": {"AAPL": {"pre_submit_max_risk_multiple": -1}}},
        {"pre_submit_symbol_caps": {"AAPL": {"pre_submit_max_qty": 0}}},
        {"pre_submit_symbol_caps": "not-a-mapping"},
        {"pre_submit_max_qtyy": 5},
    ],
)
def test_create_live_deployment_rejects_invalid_cap_settings(
    isolated_db_sandbox, settings
):
    """``create_live_deployment`` must refuse malformed cap settings.

    Pins the ``validate_pre_submit_cap_settings(deployment.settings)`` call in
    ``shared/database.py``: a mutant that drops it would store a cap the first
    order is only refused by at submit time.
    """
    database, _, _ = isolated_db_sandbox
    _seed_account(database, "acc-1")
    deployment = _deployment(**settings)

    with pytest.raises(ValueError):
        database.create_live_deployment(deployment)

    assert database.list_live_deployments() == []


def test_create_live_deployment_accepts_valid_cap_settings(isolated_db_sandbox):
    database, _, _ = isolated_db_sandbox
    _seed_account(database, "acc-1")
    deployment = _deployment(
        pre_submit_max_qty=10,
        pre_submit_max_risk_multiple=3.0,
        pre_submit_symbol_caps={"AAPL": {"pre_submit_max_qty": 4}},
    )

    created = database.create_live_deployment(deployment)

    assert created.settings["pre_submit_max_qty"] == 10
    assert created.settings["pre_submit_symbol_caps"]["AAPL"]["pre_submit_max_qty"] == 4
