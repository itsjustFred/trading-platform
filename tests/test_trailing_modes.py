"""Issue #150: trailing-stop modes are validated centrally and fail closed.

Issue #167 made live support per broker: `percent` is executed live on Alpaca
(see ``tests/test_live_trailing_stops.py``); every other broker/mode
combination is still refused at deployment validation and at signal time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from live_runner import daemon
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal
from shared.trailing import (
    KNOWN_TRAILING_MODES,
    LIVE_ANY_BROKER_TRAILING_MODES,
    LIVE_BASELINE_TRAILING_MODES,
    LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER,
    REPLAY_SUPPORTED_TRAILING_MODES,
    RISK_ENGINE_SUPPORTED_TRAILING_MODES,
    live_supported_trailing_modes,
    validate_live_deployment_trailing_override,
    validate_trailing_mode,
    validate_trailing_sl_config,
)


def _deployment(signal_config: dict | None = None) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="ramjet-live",
        account_id="acct-1",
        symbols=["AAPL"],
        timeframe="5m",
        signal_source_type="discord",
        signal_source="discord",
        signal_config=signal_config or {},
    )


# ── the central contract ─────────────────────────────────────────────────────


def test_support_sets_are_what_the_paths_can_execute() -> None:
    assert KNOWN_TRAILING_MODES == {"none", "percent", "supertrend_flip"}
    assert REPLAY_SUPPORTED_TRAILING_MODES == {"none", "percent"}
    assert RISK_ENGINE_SUPPORTED_TRAILING_MODES == {"none", "percent"}
    # Live support is per broker (#167): percent on Alpaca only.
    assert LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER == {
        "alpaca": {"none", "percent"},
        "ctrader": {"none"},
    }
    assert LIVE_BASELINE_TRAILING_MODES == {"none"}
    assert LIVE_ANY_BROKER_TRAILING_MODES == {"none", "percent"}


def test_live_modes_are_resolved_per_broker_and_fail_closed() -> None:
    assert live_supported_trailing_modes("alpaca") == {"none", "percent"}
    assert live_supported_trailing_modes(" Alpaca ") == {"none", "percent"}
    assert live_supported_trailing_modes("ctrader") == {"none"}
    # Unknown or missing broker: baseline only.
    for broker in (None, "", "ibkr"):
        assert live_supported_trailing_modes(broker) == {"none"}
    # No stop-amendment capability: baseline only, whatever the broker.
    assert live_supported_trailing_modes("alpaca", supports_stop_amendment=False) == {"none"}
    assert live_supported_trailing_modes("alpaca", supports_stop_amendment="yes") == {"none"}  # type: ignore[arg-type]
    # No live broker executes supertrend_flip (#168).
    for modes in LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER.values():
        assert "supertrend_flip" not in modes


def test_mode_is_normalised() -> None:
    mode = validate_trailing_mode(
        "  Percent ", supported=REPLAY_SUPPORTED_TRAILING_MODES, context="test"
    )
    assert mode == "percent"


@pytest.mark.parametrize("value", ["percnt", "", None, "trail"])
def test_unknown_mode_is_rejected(value) -> None:
    with pytest.raises(ValueError, match="Unknown trailing stop mode"):
        validate_trailing_mode(value, supported=KNOWN_TRAILING_MODES, context="test")


def test_known_but_unsupported_mode_is_rejected_with_its_own_message() -> None:
    with pytest.raises(ValueError, match="not supported by the replay path"):
        validate_trailing_mode(
            "supertrend_flip",
            supported=REPLAY_SUPPORTED_TRAILING_MODES,
            context="the replay path",
        )


def test_sl_config_absent_means_no_trailing_requested() -> None:
    assert (
        validate_trailing_sl_config(
            None, supported=LIVE_BASELINE_TRAILING_MODES, context="test"
        )
        is None
    )


@pytest.mark.parametrize("config", ["percent", ["percent"], {}, {"percent": 0.02}])
def test_malformed_sl_config_is_rejected(config) -> None:
    with pytest.raises(ValueError, match="trailing_sl_config"):
        validate_trailing_sl_config(
            config, supported=KNOWN_TRAILING_MODES, context="test"
        )


def test_sl_config_returns_the_supported_mode() -> None:
    mode = validate_trailing_sl_config(
        {"mode": "percent", "percent": 0.02},
        supported=REPLAY_SUPPORTED_TRAILING_MODES,
        context="test",
    )
    assert mode == "percent"


# ── deployment validation ────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["supertrend_flip", "bogus"])
def test_live_deployment_rejects_a_trailing_mode_no_live_broker_can_run(mode: str) -> None:
    with pytest.raises(ValidationError, match="trailing"):
        _deployment({"signal_processor_config": {"trailing_mode": mode}})


def test_live_deployment_model_defers_percent_to_the_broker_aware_stage() -> None:
    # The model only knows the account id. `percent` is executable on at least
    # one live broker, so it passes here and is decided where the broker is
    # known (the three tests below) and again at signal time.
    deployment = _deployment({"signal_processor_config": {"trailing_mode": "percent"}})
    assert deployment.signal_config["signal_processor_config"]["trailing_mode"] == "percent"


_PERCENT = {"signal_processor_config": {"trailing_mode": "percent"}}


@pytest.mark.parametrize("broker", ["ctrader", None, "", "ibkr"])
def test_broker_aware_override_check_rejects_percent_without_broker_support(broker) -> None:
    with pytest.raises(ValueError, match="not supported"):
        validate_live_deployment_trailing_override(_PERCENT, context="test", broker=broker)


@pytest.mark.parametrize("mode", ["supertrend_flip", "bogus"])
def test_broker_aware_override_check_rejects_unsupported_modes_on_alpaca(mode: str) -> None:
    with pytest.raises(ValueError, match="[Tt]railing"):
        validate_live_deployment_trailing_override(
            {"signal_processor_config": {"trailing_mode": mode}},
            context="test",
            broker="alpaca",
        )


def test_broker_aware_override_check_accepts_percent_on_alpaca() -> None:
    validate_live_deployment_trailing_override(_PERCENT, context="test", broker="alpaca")
    validate_live_deployment_trailing_override(
        {"signal_processor_config": {"trailing_mode": "none"}}, context="test", broker="ctrader"
    )


def test_creating_a_percent_deployment_depends_on_the_account_broker(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    database.ensure_live_account("acct-alpaca", "alpaca")
    database.ensure_live_account("acct-ctrader", "ctrader", ctid="123")

    def _percent_deployment(account_id: str) -> LiveDeploymentConfig:
        return LiveDeploymentConfig(
            strategy_name="ramjet-live",
            account_id=account_id,
            symbols=["AAPL"],
            timeframe="5m",
            signal_config=_PERCENT,
        )

    created = database.create_live_deployment(_percent_deployment("acct-alpaca"))
    assert created.signal_config == _PERCENT

    refused = _percent_deployment("acct-ctrader")
    with pytest.raises(ValueError, match="'percent' is not supported"):
        database.create_live_deployment(refused)
    assert database.get_live_deployment(str(refused.id)) is None


def test_repointing_a_percent_deployment_to_a_broker_without_support_is_refused(
    isolated_db,
) -> None:
    database, _engine, _data_dir = isolated_db
    database.ensure_live_account("acct-alpaca", "alpaca")
    database.ensure_live_account("acct-ctrader", "ctrader", ctid="123")
    deployment = database.create_live_deployment(
        LiveDeploymentConfig(
            strategy_name="ramjet-live",
            account_id="acct-alpaca",
            symbols=["AAPL"],
            timeframe="5m",
            status="PAUSED",
            signal_config=_PERCENT,
        )
    )

    with pytest.raises(ValueError, match="'percent' is not supported"):
        database.update_live_deployment_account(str(deployment.id), "acct-ctrader")

    assert database.get_live_deployment(str(deployment.id)).account_id == "acct-alpaca"


@pytest.mark.parametrize(
    ("account_id", "accepted"),
    [("alp", True), ("ct", False), ("missing", False)],
)
def test_yaml_deployments_validate_percent_against_the_account_broker(
    tmp_path, account_id: str, accepted: bool
) -> None:
    from live_runner.config_parser import parse_live_deployments_config

    config = tmp_path / "live.yaml"
    config.write_text(
        f"""
accounts:
  - id: alp
    broker: alpaca
  - id: ct
    broker: ctrader
    ctid: "123"
deployments:
  - strategy_name: ramjet-live
    account_id: {account_id}
    symbols: [AAPL]
    timeframe: 5m
    signal_config:
      signal_processor_config:
        trailing_mode: percent
""",
        encoding="utf-8",
    )

    if accepted:
        _accounts, deployments = parse_live_deployments_config(config)
        assert len(deployments) == 1
    else:
        with pytest.raises(ValueError, match="'percent' is not supported"):
            parse_live_deployments_config(config)


def test_live_deployment_accepts_mode_none_and_no_override() -> None:
    explicit = _deployment({"signal_processor_config": {"trailing_mode": "none"}})
    assert explicit.signal_config["signal_processor_config"]["trailing_mode"] == "none"
    assert _deployment({"signal_processor_config": {"atr_period": 14}}).signal_config
    assert _deployment().signal_config == {}


def test_deployment_override_check_ignores_unrelated_shapes() -> None:
    validate_live_deployment_trailing_override(None, context="test")
    validate_live_deployment_trailing_override({"signal_processor_config": "x"}, context="test")


# ── signal time, live path ───────────────────────────────────────────────────


class _Processor:
    def __init__(self, trailing_cfg) -> None:
        self._trailing_cfg = trailing_cfg

    def process_signal(self, *, raw_signal, market_data):
        metadata = dict(raw_signal.get("metadata") or {})
        if self._trailing_cfg is not None:
            metadata["trailing_sl_config"] = self._trailing_cfg
        return TradeSignal(
            action="buy",
            symbol=raw_signal["symbol"],
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            timeframe="5m",
            metadata=metadata,
        )


def _process(monkeypatch, trailing_cfg, adapter=None) -> TradeSignal:
    monkeypatch.setattr(
        daemon, "get_signal_processor", lambda name, config: _Processor(trailing_cfg)
    )
    monkeypatch.setattr(daemon, "_build_processor_market_data", lambda signal, timeframe: None)
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        timeframe="5m",
        metadata={},
    )
    if adapter is None:
        return daemon._apply_signal_processor(
            signal, _deployment({"signal_processor": "ramjet"})
        )
    return daemon._apply_signal_processor(
        signal, _deployment({"signal_processor": "ramjet"}), adapter=adapter
    )


#: What the gates read from an adapter: its broker and its declared capability.
_ALPACA_LIKE = SimpleNamespace(broker_name="alpaca", supports_stop_amendment=True)
_CTRADER_LIKE = SimpleNamespace(broker_name="ctrader", supports_stop_amendment=False)
_ALPACA_WITHOUT_AMEND = SimpleNamespace(broker_name="alpaca", supports_stop_amendment=False)


@pytest.mark.parametrize(
    "trailing_cfg",
    [
        {"mode": "percent", "percent": 0.02},
        {"mode": "supertrend_flip"},
        {"mode": "bogus"},
        "percent",
    ],
)
def test_live_signal_with_unexecutable_trailing_is_refused(monkeypatch, trailing_cfg) -> None:
    with pytest.raises(ValueError, match="[Tt]railing|trailing_sl_config"):
        _process(monkeypatch, trailing_cfg)


@pytest.mark.parametrize("adapter", [_CTRADER_LIKE, _ALPACA_WITHOUT_AMEND])
def test_live_percent_signal_is_refused_for_an_adapter_that_cannot_trail(
    monkeypatch, adapter
) -> None:
    with pytest.raises(ValueError, match="'percent' is not supported"):
        _process(monkeypatch, {"mode": "percent", "percent": 0.02}, adapter)


@pytest.mark.parametrize("trailing_cfg", [{"mode": "supertrend_flip"}, {"mode": "bogus"}])
def test_live_signal_with_a_mode_no_adapter_executes_is_refused(
    monkeypatch, trailing_cfg
) -> None:
    with pytest.raises(ValueError, match="[Tt]railing"):
        _process(monkeypatch, trailing_cfg, _ALPACA_LIKE)


def test_live_percent_signal_passes_the_processor_gate_on_a_trailing_adapter(
    monkeypatch,
) -> None:
    processed = _process(monkeypatch, {"mode": "percent", "percent": 0.02}, _ALPACA_LIKE)
    assert processed.metadata["trailing_sl_config"] == {"mode": "percent", "percent": 0.02}


@pytest.mark.parametrize("trailing_cfg", [{"mode": "none"}, None])
def test_live_signal_without_trailing_passes_the_processor_gate(
    monkeypatch, trailing_cfg
) -> None:
    processed = _process(monkeypatch, trailing_cfg)
    assert processed.stop_loss == 95.0
    assert processed.metadata["signal_processor"] == "ramjet"


class _UntouchableAdapter:
    """Any broker access before the trailing gate is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"adapter.{name} was used before the trailing gate")


def _live_signal(metadata: dict) -> LiveSignal:
    return LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


@pytest.mark.parametrize(
    "trailing_cfg", [{"mode": "percent", "percent": 0.02}, {"mode": "supertrend_flip"}]
)
def test_order_preparation_refuses_trailing_before_touching_the_broker(trailing_cfg) -> None:
    signal = _live_signal({"trailing_sl_config": trailing_cfg, "stop_loss": 95.0})
    with pytest.raises(ValueError, match="not supported"):
        asyncio.run(
            daemon.prepare_order_with_risk(_UntouchableAdapter(), _deployment(), signal)
        )


def test_order_preparation_lets_mode_none_through_to_sizing(monkeypatch) -> None:
    reached = SimpleNamespace(sizing=False)

    async def _exposure(adapter, deployment):
        reached.sizing = True
        raise RuntimeError("stop after the gate")

    monkeypatch.setattr(daemon, "_broker_exposure_estimate", _exposure)
    signal = _live_signal({"trailing_sl_config": {"mode": "none"}, "stop_loss": 95.0})
    with pytest.raises(RuntimeError, match="stop after the gate"):
        asyncio.run(
            daemon.prepare_order_with_risk(_UntouchableAdapter(), _deployment(), signal)
        )
    assert reached.sizing


@pytest.mark.parametrize(
    ("adapter", "trailing_cfg"),
    [
        (_CTRADER_LIKE, {"mode": "percent", "percent": 0.02}),
        (_ALPACA_WITHOUT_AMEND, {"mode": "percent", "percent": 0.02}),
        (_ALPACA_LIKE, {"mode": "supertrend_flip"}),
    ],
)
def test_order_preparation_refuses_trailing_the_adapter_cannot_execute(
    monkeypatch, adapter, trailing_cfg
) -> None:
    async def _exposure(adapter, deployment):
        raise AssertionError("sizing was reached past the trailing gate")

    monkeypatch.setattr(daemon, "_broker_exposure_estimate", _exposure)
    signal = _live_signal({"trailing_sl_config": trailing_cfg, "stop_loss": 95.0})
    with pytest.raises(ValueError, match="not supported"):
        asyncio.run(daemon.prepare_order_with_risk(adapter, _deployment(), signal))


def test_order_preparation_lets_percent_through_on_a_trailing_adapter(monkeypatch) -> None:
    reached = SimpleNamespace(sizing=False)

    async def _exposure(adapter, deployment):
        reached.sizing = True
        raise RuntimeError("stop after the gate")

    monkeypatch.setattr(daemon, "_broker_exposure_estimate", _exposure)
    signal = _live_signal(
        {"trailing_sl_config": {"mode": "percent", "percent": 0.02}, "stop_loss": 95.0}
    )
    with pytest.raises(RuntimeError, match="stop after the gate"):
        asyncio.run(daemon.prepare_order_with_risk(_ALPACA_LIKE, _deployment(), signal))
    assert reached.sizing
