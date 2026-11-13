"""Issue #150: trailing-stop modes are validated centrally and fail closed."""

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
    LIVE_SUPPORTED_TRAILING_MODES,
    REPLAY_SUPPORTED_TRAILING_MODES,
    RISK_ENGINE_SUPPORTED_TRAILING_MODES,
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
    # Live trailing does not exist yet (#167).
    assert LIVE_SUPPORTED_TRAILING_MODES == {"none"}


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
            None, supported=LIVE_SUPPORTED_TRAILING_MODES, context="test"
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


@pytest.mark.parametrize("mode", ["percent", "supertrend_flip", "bogus"])
def test_live_deployment_rejects_a_trailing_mode_live_cannot_run(mode: str) -> None:
    with pytest.raises(ValidationError, match="trailing"):
        _deployment({"signal_processor_config": {"trailing_mode": mode}})


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


def _process(monkeypatch, trailing_cfg) -> TradeSignal:
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
    return daemon._apply_signal_processor(
        signal, _deployment({"signal_processor": "ramjet"})
    )


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
