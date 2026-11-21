"""#150: a live deployment is refused when the trailing mode its own signal
processor emits cannot be executed on the deployment's broker.

The trailing mode is a setting of the *bot* (``signal_bots/<bot>/config.yaml``),
which is where the issue's defect came from: the processor emits the mode with
every signal, so a bot whose default asks for something the live path cannot
execute used to be invisible until the first signal was refused at runtime.
Deployment validation (create, account re-point, YAML parse) now reads that mode
from the processor the deployment actually runs, and the daemon says so loudly
at load for rows that were written before the check existed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from live_runner import daemon
from shared.database import LiveDeploymentModel
from shared.models import LiveAccountConfig, LiveDeploymentConfig
from shared.trailing import validate_deployment_signal_processor_trailing_mode

#: The complete ``supertrend_flip`` parameter set the RamJet processor emits.
_FLIP_OVERRIDES = {
    "trailing_mode": "supertrend_flip",
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
}


def _bot_defaulting_to(monkeypatch, **overrides: object) -> None:
    """As if ``signal_bots/ramjet/config.yaml`` carried these settings.

    The registry merges the bot's config file with the deployment's overrides
    and hands the payload to this factory, so the factory sees exactly what the
    daemon would build. Patching the factory entry is the same seam the
    health-check tests use.
    """
    from signal_bots.ramjet.processor import RamJetSignalProcessor
    from signal_bots.registry import SIGNAL_PROCESSORS

    def factory(config: dict) -> RamJetSignalProcessor:
        return RamJetSignalProcessor({**config, **overrides})

    monkeypatch.setitem(SIGNAL_PROCESSORS, "ramjet", factory)


def _deployment(
    signal_config: dict | None = None,
    *,
    timeframe: str = "5m",
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="ramjet-live",
        account_id="acct-1",
        symbols=["AAPL"],
        timeframe=timeframe,
        signal_source_type="discord",
        signal_source="discord",
        signal_config=signal_config or {},
    )


def _check(deployment: LiveDeploymentConfig, *, broker: str = "alpaca") -> None:
    validate_deployment_signal_processor_trailing_mode(
        deployment,
        broker=broker,
        timeframe=deployment.timeframe,
        context="test",
    )


# ── the bot's own default trailing mode ──────────────────────────────────────


def test_a_bot_default_no_live_broker_executes_is_refused(monkeypatch) -> None:
    _bot_defaulting_to(monkeypatch, **_FLIP_OVERRIDES)
    deployment = _deployment({"signal_processor": "ramjet"})

    with pytest.raises(ValueError, match="supertrend_flip"):
        _check(deployment)
    with pytest.raises(ValueError, match="supertrend_flip"):
        _check(deployment, broker="ctrader")
    # An unknown broker trails nothing but "none": fail closed, not open.
    with pytest.raises(ValueError, match="supertrend_flip"):
        _check(deployment, broker="mystery-broker")


def test_the_shipped_bot_default_is_accepted(monkeypatch) -> None:
    """Nothing is patched here: the shipped ``config.yaml`` default is ``none``."""
    _check(_deployment({"signal_processor": "ramjet"}))
    _check(_deployment({"signal_processor": "ramjet"}), broker="ctrader")


def test_a_percent_bot_default_is_broker_aware(monkeypatch) -> None:
    _bot_defaulting_to(monkeypatch, trailing_mode="percent", trailing_percent=0.02)
    deployment = _deployment({"signal_processor": "ramjet"})

    _check(deployment, broker="alpaca")
    with pytest.raises(ValueError, match="not supported"):
        _check(deployment, broker="ctrader")


def test_a_percent_bot_default_needs_a_trailable_timeframe(monkeypatch) -> None:
    """#150: an unservable trail is refused at deployment validation, not later."""
    _bot_defaulting_to(monkeypatch, trailing_mode="percent", trailing_percent=0.02)
    deployment = _deployment({"signal_processor": "ramjet"}, timeframe="1d")

    with pytest.raises(ValueError, match="cannot be executed live on timeframe"):
        _check(deployment, broker="alpaca")
    _check(_deployment({"signal_processor": "ramjet"}), broker="alpaca")


def test_an_explicit_override_is_left_to_the_override_check(monkeypatch) -> None:
    """The override check already gates an explicit mode; this one must not
    rebuild the processor for it (and must not double-report it)."""
    from signal_bots.registry import SIGNAL_PROCESSORS

    def exploding_factory(config: dict) -> object:
        raise AssertionError("an explicit override must not build the processor")

    monkeypatch.setitem(SIGNAL_PROCESSORS, "ramjet", exploding_factory)
    deployment = _deployment(
        {
            "signal_processor": "ramjet",
            "signal_processor_config": {"trailing_mode": "percent", "trailing_percent": 0.02},
        }
    )

    _check(deployment, broker="alpaca")


def test_a_deployment_without_a_processor_is_unaffected(monkeypatch) -> None:
    _bot_defaulting_to(monkeypatch, **_FLIP_OVERRIDES)
    # A raw-channel deployment trades the Discord signal itself: no processor,
    # so no processor default to check.
    _check(_deployment({"channel_id": "123"}))


def test_a_processor_that_declares_no_trailing_mode_is_unaffected(monkeypatch) -> None:
    """RocketFuel has no trailing model at all; nothing to check."""
    _bot_defaulting_to(monkeypatch, **_FLIP_OVERRIDES)
    _check(_deployment({"signal_processor": "rocketfuel_longsonly"}))


def test_an_unregistered_processor_is_a_deployment_config_error() -> None:
    with pytest.raises(ValueError, match="Unknown signal processor"):
        _check(_deployment({"signal_processor": "mystery"}))


# ── the deployment-validation stages ─────────────────────────────────────────


def _ramjet_account(database) -> None:
    """The Alpaca account the RamJet StockKit route points at."""
    from live_runner.stockkit_routing import STOCKKIT_ACCOUNT_ROUTES

    database.ensure_live_account(
        "acct-alpaca", "alpaca", broker_login=STOCKKIT_ACCOUNT_ROUTES["ramjet"]
    )


def test_creating_a_deployment_whose_bot_default_live_cannot_run_is_refused(
    isolated_db, monkeypatch
) -> None:
    database, _engine, _data_dir = isolated_db
    _ramjet_account(database)
    _bot_defaulting_to(monkeypatch, **_FLIP_OVERRIDES)
    deployment = _deployment({"signal_processor": "ramjet"})
    deployment.account_id = "acct-alpaca"

    with pytest.raises(ValueError, match="supertrend_flip"):
        database.create_live_deployment(deployment)
    assert database.get_live_deployment(str(deployment.id)) is None


def test_yaml_deployment_whose_bot_default_live_cannot_run_is_refused(
    tmp_path, monkeypatch
) -> None:
    from live_runner.config_parser import parse_live_deployments_config

    _bot_defaulting_to(monkeypatch, **_FLIP_OVERRIDES)
    config = tmp_path / "live.yaml"
    config.write_text(
        """
accounts:
  - id: alp
    broker: alpaca

deployments:
  - strategy_name: ramjet-live
    account_id: alp
    symbols: [AAPL]
    timeframe: 5m
    status: PAUSED
    signal_source: discord
    signal_config:
      channel_id: "123"
      signal_processor: ramjet
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="supertrend_flip"):
        parse_live_deployments_config(config)


def test_re_pointing_to_a_broker_that_cannot_run_the_bot_default_is_refused(
    isolated_db, monkeypatch
) -> None:
    database, _engine, _data_dir = isolated_db
    _ramjet_account(database)
    database.ensure_live_account("acct-ctrader", "ctrader", ctid="123")
    _bot_defaulting_to(monkeypatch, trailing_mode="percent", trailing_percent=0.02)
    deployment = _deployment({"signal_processor": "ramjet"})
    deployment.account_id = "acct-alpaca"
    database.create_live_deployment(deployment)

    with pytest.raises(ValueError, match="not supported"):
        database.update_live_deployment_account(str(deployment.id), "acct-ctrader")


# ── stored rows: never break the list, always say so ────────────────────────


def _stored_row(signal_config: dict, timeframe: str = "5m") -> LiveDeploymentModel:
    return LiveDeploymentModel(
        id="6f1f0c52-2f0e-4e0e-9a55-3f6f2c3a1b7d",
        strategy_name="ramjet-live",
        account_id="acct-1",
        symbols_json=["AAPL"],
        timeframe=timeframe,
        status="ACTIVE",
        signal_config_json=signal_config,
    )


def test_stored_row_with_a_flip_bot_default_still_loads_and_logs_an_error(
    monkeypatch,
) -> None:
    """A row written before this check existed must not make the list raise."""
    _bot_defaulting_to(monkeypatch, **_FLIP_OVERRIDES)
    row = _stored_row({"signal_processor": "ramjet"})
    deployment = row.to_live_deployment()
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    monkeypatch.setattr(
        daemon,
        "list_live_accounts",
        lambda: [LiveAccountConfig(id="acct-1", broker="alpaca")],
    )
    monkeypatch.setattr(daemon, "list_live_deployments", lambda: [deployment])

    _accounts, loaded = asyncio.run(daemon._load_db_accounts_and_deployments())

    assert [str(item.id) for item in loaded] == [str(deployment.id)]
    assert len(errors) == 1
    msg, args, _kwargs = errors[0]
    assert "unsupported trailing mode" in msg
    assert "supertrend_flip" in str(args)


def test_stored_row_with_a_working_bot_default_loads_quietly(monkeypatch) -> None:
    row = _stored_row({"signal_processor": "ramjet"})
    deployment = row.to_live_deployment()
    errors: list[tuple] = []
    monkeypatch.setattr(
        daemon,
        "logger",
        SimpleNamespace(error=lambda msg, *args, **kwargs: errors.append((msg, args, kwargs))),
    )
    monkeypatch.setattr(
        daemon,
        "list_live_accounts",
        lambda: [LiveAccountConfig(id="acct-1", broker="alpaca")],
    )
    monkeypatch.setattr(daemon, "list_live_deployments", lambda: [deployment])

    asyncio.run(daemon._load_db_accounts_and_deployments())

    assert errors == []
