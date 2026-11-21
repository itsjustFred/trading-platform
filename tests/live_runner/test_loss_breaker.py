"""Tests for the live loss circuit breaker (#301).

Pins the maintainer decision: daily loss 5 % of start-of-day equity and
overall drawdown 20 % from peak, per account and per deployment, configurable;
PAUSED semantics (entries blocked, exits allowed); daily auto-reset at the UTC
day boundary; drawdown reset only by the explicit operator reset; fail closed
on unreadable equity or state.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import live_runner.daemon as daemon
import live_runner.loss_breaker as loss_breaker
from live_runner.base_adapter import LiveQuote
from live_runner.loss_breaker import (
    DEFAULT_DAILY_LOSS_FRACTION,
    DEFAULT_MAX_DRAWDOWN_FRACTION,
    LossBreakerBreachError,
    account_loss_breaker_limits,
    account_loss_breaker_scope,
    deployment_loss_breaker_limits,
    deployment_loss_breaker_scope,
    observe_account_loss_breaker_equity,
    refuse_entry_if_loss_breaker_tripped,
)
from shared.database import (
    get_latest_live_balance,
    get_live_loss_breaker_state,
    list_live_loss_breaker_states,
    observe_live_loss_breaker,
    save_live_balance,
)
from shared.models import LiveBalanceRecord, LiveDeploymentConfig, LiveSignal

NO_ENV = {}
DAY1 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)


def _deployment(
    account_id: str = "acc-1",
    settings: dict | None = None,
    deployment_id: str | None = None,
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=deployment_id or str(uuid4()),
        strategy_name="demo",
        account_id=account_id,
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings=settings or {},
    )


def _observe(scope_account: str, equity: float, when: datetime) -> None:
    observe_live_loss_breaker(
        account_loss_breaker_scope(scope_account), scope_account, equity, when
    )


def _observe_deployment(deployment: LiveDeploymentConfig, equity: float, when: datetime):
    return observe_live_loss_breaker(
        deployment_loss_breaker_scope(str(deployment.id)),
        deployment.account_id,
        equity,
        when,
    )


def _entry_signal(deployment: LiveDeploymentConfig) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0, "take_profit": 110.0},
    )


class _Adapter:
    demo = True

    def __init__(self, equity: float) -> None:
        self._equity = equity

    async def get_balance(self):
        return self._equity, self._equity

    async def get_positions(self):
        return []

    async def get_quote(self, symbol):
        return LiveQuote(bid=100.0, ask=100.0, timestamp=datetime.now(timezone.utc))

    async def get_pip_value(self, symbol):
        from live_runner.base_adapter import LivePipValue

        return LivePipValue(
            value=1.0, units_per_qty=1.0, conversion_rate=1.0,
            quote_asset="USD", account_asset="USD", source="test",
        )


# ─── Configuration ────────────────────────────────────────────────────────────


def test_default_limits_are_the_maintainer_thresholds():
    limits = account_loss_breaker_limits("acc-1", env_values=NO_ENV)
    assert limits.enabled is True
    assert limits.daily_loss_fraction == DEFAULT_DAILY_LOSS_FRACTION == pytest.approx(0.05)
    assert limits.max_drawdown_fraction == DEFAULT_MAX_DRAWDOWN_FRACTION == pytest.approx(0.20)


def test_per_account_env_overrides_global_env():
    env = {
        "LOSS_BREAKER_DAILY_LOSS_FRACTION": "0.10",
        "LOSS_BREAKER_ACME_DAILY_LOSS_FRACTION": "0.02",
        "LOSS_BREAKER_ACME_MAX_DRAWDOWN_FRACTION": "0.30",
    }
    acme = account_loss_breaker_limits("acme", env_values=env)
    assert acme.daily_loss_fraction == pytest.approx(0.02)
    assert acme.max_drawdown_fraction == pytest.approx(0.30)
    other = account_loss_breaker_limits("other", env_values=env)
    assert other.daily_loss_fraction == pytest.approx(0.10)
    assert other.max_drawdown_fraction == pytest.approx(0.20)


def test_deployment_settings_override_account_limits_per_key():
    deployment = _deployment(
        settings={"loss_breaker_daily_loss_fraction": 0.01}
    )
    limits = deployment_loss_breaker_limits(deployment, env_values=NO_ENV)
    assert limits.daily_loss_fraction == pytest.approx(0.01)
    # The unset key still inherits the account-level (default) value.
    assert limits.max_drawdown_fraction == pytest.approx(0.20)


@pytest.mark.parametrize(
    "settings",
    [
        {"loss_breaker_daily_loss_fraction": 0},
        {"loss_breaker_daily_loss_fraction": 1.5},
        {"loss_breaker_daily_loss_fraction": "x"},
        {"loss_breaker_max_drawdown_fraction": -0.1},
        {"loss_breaker_enabled": "maybe"},
    ],
)
def test_invalid_deployment_settings_fail_closed(settings):
    deployment = _deployment(settings=settings)
    with pytest.raises(ValueError):
        deployment_loss_breaker_limits(deployment, env_values=NO_ENV)


def test_fraction_bounds_are_inclusive_of_one():
    deployment = _deployment(
        settings={
            "loss_breaker_daily_loss_fraction": 1.0,
            "loss_breaker_max_drawdown_fraction": "0.5",
        }
    )
    limits = deployment_loss_breaker_limits(deployment, env_values=NO_ENV)
    assert limits.daily_loss_fraction == pytest.approx(1.0)
    assert limits.max_drawdown_fraction == pytest.approx(0.5)


# ─── Daily breaker ────────────────────────────────────────────────────────────


def test_daily_breaker_trips_at_five_percent_and_refuses_entries():
    deployment = _deployment(account_id="acc-daily")
    scope = account_loss_breaker_scope("acc-daily")
    # First observation sets the start-of-day baseline.
    _observe("acc-daily", 100_000.0, DAY1)
    # Equity down exactly 5 %: the threshold is inclusive.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=95_000.0, now=DAY1, env_values=NO_ENV
        )
    assert excinfo.value.kind == "daily"
    assert excinfo.value.scope == scope
    state = get_live_loss_breaker_state(scope)
    assert state.daily_breached is True


def test_daily_breaker_allows_losses_below_threshold():
    deployment = _deployment(account_id="acc-ok")
    _observe("acc-ok", 100_000.0, DAY1)
    # 4.9 % down: under the 5 % daily limit and far under the drawdown limit.
    refuse_entry_if_loss_breaker_tripped(
        deployment, equity=95_100.0, now=DAY1, env_values=NO_ENV
    )


def test_daily_latch_holds_even_if_equity_recovers_same_day():
    deployment = _deployment(account_id="acc-latch")
    _observe("acc-latch", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=94_000.0, now=DAY1, env_values=NO_ENV
        )
    # Equity back above the threshold: the latch still refuses - the daily
    # breaker resets at the UTC boundary, not on recovery.
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=99_000.0, now=DAY1 + timedelta(hours=1), env_values=NO_ENV
        )


def test_daily_breaker_resets_at_utc_day_boundary():
    deployment = _deployment(account_id="acc-roll")
    _observe("acc-roll", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    # Next UTC day: the daily latch clears and the baseline re-bases to the
    # last pre-boundary equity (90 000), so the same equity is not a "daily"
    # loss any more.
    refuse_entry_if_loss_breaker_tripped(
        deployment, equity=90_000.0, now=DAY2, env_values=NO_ENV
    )
    state = get_live_loss_breaker_state(account_loss_breaker_scope("acc-roll"))
    assert state.daily_breached is False
    assert state.utc_day == "2026-11-21"


def test_overnight_gap_counts_against_the_new_day():
    """The day baseline is the last pre-boundary equity: a gap down across
    midnight IS a daily loss, not a fresh start."""
    deployment = _deployment(account_id="acc-gap")
    _observe("acc-gap", 100_000.0, DAY1)
    # 10 % gap down observed just after the UTC boundary.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=90_000.0, now=DAY2, env_values=NO_ENV
        )
    assert excinfo.value.kind == "daily"


# ─── Drawdown breaker ──────────────────────────────────────────────────────────


def test_drawdown_breaker_trips_twenty_percent_below_peak():
    deployment = _deployment(account_id="acc-dd")
    _observe("acc-dd", 100_000.0, DAY1)
    _observe("acc-dd", 120_000.0, DAY1 + timedelta(hours=1))  # new peak
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=95_999.0, now=DAY1 + timedelta(hours=2), env_values=NO_ENV
        )
    assert excinfo.value.kind == "drawdown"
    state = get_live_loss_breaker_state(account_loss_breaker_scope("acc-dd"))
    assert state.drawdown_breached is True
    assert state.peak_equity == pytest.approx(120_000.0)


def test_drawdown_latch_survives_equity_recovery_and_day_rollover():
    deployment = _deployment(account_id="acc-dd2")
    _observe("acc-dd2", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=79_000.0, now=DAY1, env_values=NO_ENV
        )
    # 21 % under day-start AND 21 % under peak: BOTH latches must be set -
    # latching only the daily one would silently clear the drawdown breach at
    # the UTC rollover.
    state = get_live_loss_breaker_state(account_loss_breaker_scope("acc-dd2"))
    assert state.daily_breached is True
    assert state.drawdown_breached is True
    # Recovery and a new UTC day do NOT clear the drawdown latch: only the
    # explicit operator reset does.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=100_000.0, now=DAY2, env_values=NO_ENV
        )
    assert excinfo.value.kind == "drawdown"


def test_drawdown_operator_reset_clears_latch_and_rebases_peak():
    from live_runner.loss_breaker import reset_drawdown_breaker

    deployment = _deployment(account_id="acc-dd3")
    scope = account_loss_breaker_scope("acc-dd3")
    _observe("acc-dd3", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=79_000.0, now=DAY1, env_values=NO_ENV
        )
    updated = reset_drawdown_breaker(scope, equity=80_000.0, now=DAY1)
    assert updated.drawdown_breached is False
    assert updated.peak_equity == pytest.approx(80_000.0)
    # The reset is drawdown-only: the daily latch from the same crash still
    # stands, so entries stay refused the rest of the UTC day.
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=80_000.0, now=DAY1, env_values=NO_ENV
        )
    # Next UTC day the daily breaker resets; with the drawdown latch cleared
    # and the peak re-based to 80k, entries are allowed again - the old 100k
    # high-water mark cannot re-trip the breaker.
    refuse_entry_if_loss_breaker_tripped(
        deployment, equity=80_000.0, now=DAY2, env_values=NO_ENV
    )
    # But a fresh 20 % drawdown from the NEW peak still trips.
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=63_000.0, now=DAY2, env_values=NO_ENV
        )


def test_reset_drawdown_on_unknown_scope_returns_none():
    from live_runner.loss_breaker import reset_drawdown_breaker

    assert (
        reset_drawdown_breaker("account:nope", equity=1.0, now=DAY1) is None
    )


# ─── Per-account vs per-deployment ─────────────────────────────────────────────


def test_account_breach_blocks_every_deployment_on_the_account():
    dep_a = _deployment(account_id="acc-shared")
    dep_b = _deployment(account_id="acc-shared")
    _observe("acc-shared", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            dep_a, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    # A second deployment on the same account is refused by the ACCOUNT
    # scope even though its own deployment scope never breached.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            dep_b, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    assert excinfo.value.scope == account_loss_breaker_scope("acc-shared")


def test_deployment_breach_blocks_only_that_deployment():
    tight = _deployment(
        account_id="acc-mix",
        settings={"loss_breaker_daily_loss_fraction": 0.01},
    )
    loose = _deployment(account_id="acc-mix")
    _observe("acc-mix", 100_000.0, DAY1)
    # 2 % down: over the tight deployment's 1 % daily limit, under the
    # account's 5 %.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            tight, equity=98_000.0, now=DAY1, env_values=NO_ENV
        )
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(tight.id))
    # The sibling deployment on the same account still trades.
    refuse_entry_if_loss_breaker_tripped(
        loose, equity=98_000.0, now=DAY1, env_values=NO_ENV
    )


def test_other_accounts_are_unaffected():
    dep = _deployment(account_id="acc-hit")
    other = _deployment(account_id="acc-safe")
    _observe("acc-hit", 100_000.0, DAY1)
    _observe("acc-safe", 50_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        refuse_entry_if_loss_breaker_tripped(
            dep, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    refuse_entry_if_loss_breaker_tripped(
        other, equity=50_000.0, now=DAY1, env_values=NO_ENV
    )


def test_disabled_deployment_scope_never_latches_but_account_still_enforces():
    deployment = _deployment(
        account_id="acc-off",
        settings={"loss_breaker_enabled": False},
    )
    _observe("acc-off", 100_000.0, DAY1)
    # A quiet observation creates the deployment row (baseline stays warm for
    # a re-enable) without latching it.
    refuse_entry_if_loss_breaker_tripped(
        deployment, equity=99_000.0, now=DAY1, env_values=NO_ENV
    )
    # 10 % down: the ACCOUNT breaker (5 %) refuses; the disabled deployment
    # scope never latches on its own.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    assert excinfo.value.scope == account_loss_breaker_scope("acc-off")
    dep_state = get_live_loss_breaker_state(
        deployment_loss_breaker_scope(str(deployment.id))
    )
    assert dep_state is not None
    assert dep_state.daily_breached is False
    assert dep_state.drawdown_breached is False


def test_fully_disabled_breaker_never_writes_state():
    deployment = _deployment(
        account_id="acc-off2",
        settings={"loss_breaker_enabled": False},
    )
    env = {"LOSS_BREAKER_ENABLED": "false"}
    refuse_entry_if_loss_breaker_tripped(
        deployment, equity=50_000.0, now=DAY1, env_values=env
    )
    assert list_live_loss_breaker_states("acc-off2") == []


# ─── Fail closed ───────────────────────────────────────────────────────────────


def test_unreadable_equity_refuses_entries():
    deployment = _deployment(account_id="acc-bad-eq")
    for bad in (0.0, -5.0, float("nan"), float("inf")):
        with pytest.raises(LossBreakerBreachError) as excinfo:
            refuse_entry_if_loss_breaker_tripped(
                deployment, equity=bad, now=DAY1, env_values=NO_ENV
            )
        assert excinfo.value.state_unreadable is True


def test_unreadable_state_refuses_entries(monkeypatch):
    deployment = _deployment(account_id="acc-bad-state")

    def _boom(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(loss_breaker, "observe_live_loss_breaker", _boom)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=100_000.0, now=DAY1, env_values=NO_ENV
        )
    assert excinfo.value.state_unreadable is True


def test_nonpositive_stored_baseline_refuses_entries():
    deployment = _deployment(account_id="acc-neg")
    # Hand-corrupt the baseline the way a bad migration or manual edit could.
    observe_live_loss_breaker(
        account_loss_breaker_scope("acc-neg"), "acc-neg", 100_000.0, DAY1
    )
    from shared.database import LiveLossBreakerStateModel, get_session

    scope = account_loss_breaker_scope("acc-neg")
    with get_session() as session:
        row = session.get(LiveLossBreakerStateModel, scope)
        row.day_start_equity = 0.0
        session.add(row)
        session.commit()
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            deployment, equity=100_000.0, now=DAY1, env_values=NO_ENV
        )
    assert excinfo.value.state_unreadable is True


# ─── Alert path ────────────────────────────────────────────────────────────────


def test_breach_alerts_once_per_episode(caplog):
    deployment = _deployment(account_id="acc-alert")
    _observe("acc-alert", 100_000.0, DAY1)
    logger = loss_breaker.logger
    with caplog.at_level(logging.ERROR, logger=logger.name):
        for _ in range(3):
            with pytest.raises(LossBreakerBreachError):
                refuse_entry_if_loss_breaker_tripped(
                    deployment, equity=90_000.0, now=DAY1, env_values=NO_ENV
                )
    alerts = [
        r for r in caplog.records
        if getattr(r, "data", {}).get("alert") == "loss_breaker_breach"
    ]
    assert len(alerts) == 1
    assert alerts[0].data["kind"] == "daily"
    assert alerts[0].data["account_id"] == "acc-alert"


# ─── Poll-path observation ─────────────────────────────────────────────────────


def test_poll_observation_trips_breaker_without_any_entry():
    dep_a = _deployment(account_id="acc-poll")
    dep_b = _deployment(account_id="acc-poll")
    observe_account_loss_breaker_equity(
        "acc-poll",
        100_000.0,
        now=DAY1,
        deployments=[dep_a, dep_b],
        env_values=NO_ENV,
    )
    # Second poll: equity down 6 % - both scopes latch on the poll itself.
    observe_account_loss_breaker_equity(
        "acc-poll",
        94_000.0,
        now=DAY1 + timedelta(minutes=1),
        deployments=[dep_a, dep_b],
        env_values=NO_ENV,
    )
    for dep in (dep_a, dep_b):
        with pytest.raises(LossBreakerBreachError):
            refuse_entry_if_loss_breaker_tripped(
                dep, equity=94_000.0, now=DAY1, env_values=NO_ENV
            )
    state = get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep_a.id)))
    assert state.daily_breached is True


def test_poll_observation_ignores_deployments_of_other_accounts():
    mine = _deployment(account_id="acc-p1")
    theirs = _deployment(account_id="acc-p2")
    observe_account_loss_breaker_equity(
        "acc-p1",
        100_000.0,
        now=DAY1,
        deployments=[mine, theirs],
        env_values=NO_ENV,
    )
    scopes = {row.scope for row in list_live_loss_breaker_states("acc-p1")}
    assert deployment_loss_breaker_scope(str(mine.id)) in scopes
    assert deployment_loss_breaker_scope(str(theirs.id)) not in scopes


def test_poll_observation_skips_unusable_equity(caplog):
    observe_account_loss_breaker_equity(
        "acc-p3", float("nan"), now=DAY1, deployments=[], env_values=NO_ENV
    )
    assert list_live_loss_breaker_states("acc-p3") == []


# ─── Daemon entry gate ─────────────────────────────────────────────────────────


def test_prepare_order_with_risk_refuses_entry_when_breaker_tripped():
    account_id = "acc-daemon"
    deployment = _deployment(account_id=account_id)
    _observe(account_id, 100_000.0, DAY1)
    adapter = _Adapter(equity=90_000.0)
    with pytest.raises(LossBreakerBreachError):
        asyncio.run(
            daemon.prepare_order_with_risk(
                adapter, deployment, _entry_signal(deployment)
            )
        )
    assert adapter_submitted(adapter) == []


def adapter_submitted(adapter):
    return getattr(adapter, "submitted", [])


def test_prepare_order_with_risk_allows_entry_under_threshold():
    account_id = "acc-daemon-ok"
    deployment = _deployment(account_id=account_id)
    _observe(account_id, 100_000.0, DAY1)
    adapter = _Adapter(equity=99_000.0)
    prepared, qty = asyncio.run(
        daemon.prepare_order_with_risk(
            adapter, deployment, _entry_signal(deployment)
        )
    )
    assert qty > 0
    assert prepared is not None


def test_breaker_never_blocks_exits():
    """An exit order returns through the exit branch before the breaker runs."""
    account_id = "acc-exit"
    deployment = _deployment(account_id=account_id)
    _observe(account_id, 100_000.0, DAY1)
    # Trip the breaker hard.
    observe_live_loss_breaker(
        account_loss_breaker_scope(account_id), account_id, 50_000.0, DAY1
    )
    exit_signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "signal_intent": "exit",
            "exit_fraction": 1.0,
            "exit_only": True,
        },
    )
    adapter = _Adapter(equity=50_000.0)
    # The exit path raises its own domain error (nothing to exit / broker
    # state), never LossBreakerBreachError.
    try:
        asyncio.run(
            daemon.prepare_order_with_risk(adapter, deployment, exit_signal)
        )
    except LossBreakerBreachError:
        pytest.fail("exit order was blocked by the loss breaker")
    except Exception:
        pass


# ─── Balance snapshot helper used by the CLI reset ─────────────────────────────


def test_latest_balance_lookup_for_reset():
    record = LiveBalanceRecord(
        account_id="acc-bal",
        timestamp=datetime.now(timezone.utc),
        balance=100.0,
        equity=95.5,
    )
    save_live_balance(record)
    latest = get_latest_live_balance("acc-bal")
    assert latest is not None
    assert latest.equity == pytest.approx(95.5)
