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
    save_live_execution,
    save_live_signal,
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


def _gate(
    deployment: LiveDeploymentConfig,
    *,
    equity: float,
    now: datetime,
    env_values: dict | None = None,
    symbol: str | None = None,
    deployment_equity: float | None = None,
) -> None:
    """The entry gate with ``deployment_equity`` defaulting to ``equity``.

    These tests have no other deployments' P&L, so the deployment scope's
    synthetic equity equals the account equity; tests that exercise the
    P&L-adjusted scope pass ``deployment_equity`` or call
    ``refuse_entry_if_loss_breaker_tripped`` directly.
    """
    return refuse_entry_if_loss_breaker_tripped(
        deployment,
        equity=equity,
        deployment_equity=equity if deployment_equity is None else deployment_equity,
        now=now,
        env_values=NO_ENV if env_values is None else env_values,
        symbol=symbol,
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
        _gate(
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
    _gate(
        deployment, equity=95_100.0, now=DAY1, env_values=NO_ENV
    )


def test_daily_latch_holds_even_if_equity_recovers_same_day():
    deployment = _deployment(account_id="acc-latch")
    _observe("acc-latch", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        _gate(
            deployment, equity=94_000.0, now=DAY1, env_values=NO_ENV
        )
    # Equity back above the threshold: the latch still refuses - the daily
    # breaker resets at the UTC boundary, not on recovery.
    with pytest.raises(LossBreakerBreachError):
        _gate(
            deployment, equity=99_000.0, now=DAY1 + timedelta(hours=1), env_values=NO_ENV
        )


def test_daily_breaker_resets_at_utc_day_boundary():
    deployment = _deployment(account_id="acc-roll")
    _observe("acc-roll", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        _gate(
            deployment, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    # Next UTC day: the daily latch clears and the baseline re-bases to the
    # last pre-boundary equity (90 000), so the same equity is not a "daily"
    # loss any more.
    _gate(
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
        _gate(
            deployment, equity=90_000.0, now=DAY2, env_values=NO_ENV
        )
    assert excinfo.value.kind == "daily"


# ─── Drawdown breaker ──────────────────────────────────────────────────────────


def test_drawdown_breaker_trips_twenty_percent_below_peak():
    deployment = _deployment(account_id="acc-dd")
    _observe("acc-dd", 100_000.0, DAY1)
    _observe("acc-dd", 120_000.0, DAY1 + timedelta(hours=1))  # new peak
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
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
        _gate(
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
        _gate(
            deployment, equity=100_000.0, now=DAY2, env_values=NO_ENV
        )
    assert excinfo.value.kind == "drawdown"


def test_drawdown_operator_reset_clears_latch_and_rebases_peak():
    from live_runner.loss_breaker import reset_drawdown_breaker

    deployment = _deployment(account_id="acc-dd3")
    scope = account_loss_breaker_scope("acc-dd3")
    _observe("acc-dd3", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        _gate(
            deployment, equity=79_000.0, now=DAY1, env_values=NO_ENV
        )
    updated = reset_drawdown_breaker(scope, equity=80_000.0, now=DAY1)
    assert updated.drawdown_breached is False
    assert updated.peak_equity == pytest.approx(80_000.0)
    # The reset is drawdown-only: the daily latch from the same crash still
    # stands, so entries stay refused the rest of the UTC day.
    with pytest.raises(LossBreakerBreachError):
        _gate(
            deployment, equity=80_000.0, now=DAY1, env_values=NO_ENV
        )
    # Next UTC day the daily breaker resets; with the drawdown latch cleared
    # and the peak re-based to 80k, entries are allowed again - the old 100k
    # high-water mark cannot re-trip the breaker.
    _gate(
        deployment, equity=80_000.0, now=DAY2, env_values=NO_ENV
    )
    # But a fresh 20 % drawdown from the NEW peak still trips.
    with pytest.raises(LossBreakerBreachError):
        _gate(
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
        _gate(
            dep_a, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    # A second deployment on the same account is refused by the ACCOUNT
    # scope even though its own deployment scope never breached.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
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
    # The deployment scope seeds its baseline from its OWN first observation
    # (flat P&L -> V == account equity), then measures the drop against it.
    _gate(tight, equity=100_000.0, now=DAY1, env_values=NO_ENV)
    # 2 % down: over the tight deployment's 1 % daily limit, under the
    # account's 5 %.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            tight, equity=98_000.0, now=DAY1 + timedelta(minutes=1), env_values=NO_ENV
        )
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(tight.id))
    # The sibling deployment on the same account still trades: its scope
    # seeds at the current 98k (its own first observation) and the account
    # scope is only -2 %.
    _gate(
        loose, equity=98_000.0, now=DAY1 + timedelta(minutes=1), env_values=NO_ENV
    )


def test_other_accounts_are_unaffected():
    dep = _deployment(account_id="acc-hit")
    other = _deployment(account_id="acc-safe")
    _observe("acc-hit", 100_000.0, DAY1)
    _observe("acc-safe", 50_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError):
        _gate(
            dep, equity=90_000.0, now=DAY1, env_values=NO_ENV
        )
    _gate(
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
    _gate(
        deployment, equity=99_000.0, now=DAY1, env_values=NO_ENV
    )
    # 10 % down: the ACCOUNT breaker (5 %) refuses; the disabled deployment
    # scope never latches on its own.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
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
    _gate(
        deployment, equity=50_000.0, now=DAY1, env_values=env
    )
    assert list_live_loss_breaker_states("acc-off2") == []


# ─── Fail closed ───────────────────────────────────────────────────────────────


def test_unreadable_equity_refuses_entries():
    deployment = _deployment(account_id="acc-bad-eq")
    for bad in (0.0, -5.0, float("nan"), float("inf")):
        with pytest.raises(LossBreakerBreachError) as excinfo:
            _gate(
                deployment, equity=bad, now=DAY1, env_values=NO_ENV
            )
        assert excinfo.value.state_unreadable is True


def test_unreadable_state_refuses_entries(monkeypatch):
    deployment = _deployment(account_id="acc-bad-state")

    def _boom(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(loss_breaker, "observe_live_loss_breaker", _boom)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
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
        _gate(
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
                _gate(
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
        deployment_pnls={},
        env_values=NO_ENV,
    )
    # Second poll: equity down 6 % - both scopes latch on the poll itself.
    observe_account_loss_breaker_equity(
        "acc-poll",
        94_000.0,
        now=DAY1 + timedelta(minutes=1),
        deployments=[dep_a, dep_b],
        deployment_pnls={},
        env_values=NO_ENV,
    )
    for dep in (dep_a, dep_b):
        with pytest.raises(LossBreakerBreachError):
            _gate(
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
        deployment_pnls={},
        env_values=NO_ENV,
    )
    scopes = {row.scope for row in list_live_loss_breaker_states("acc-p1")}
    assert deployment_loss_breaker_scope(str(mine.id)) in scopes
    assert deployment_loss_breaker_scope(str(theirs.id)) not in scopes


def test_poll_observation_skips_unusable_equity(caplog):
    observe_account_loss_breaker_equity(
        "acc-p3", float("nan"), now=DAY1, deployments=[], deployment_pnls={}, env_values=NO_ENV
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


# ─── Deployment scope: the deployment's OWN P&L (#301 review) ─────────────────
#
# A deployment scope does not observe raw account equity: it observes
# ``account_equity - sum(other deployments' P&L)`` so it moves only with its
# own deployment's realized+unrealized P&L. These tests pin that a deployment
# losing money trips ITS breaker even while the account net is fine, and that
# an unmeasurable deployment P&L fails closed.


def test_deployment_scope_trips_on_own_pnl_while_account_stays_under():
    """The reviewer's scenario: A loses 6%, B gains 3%, account nets -3%."""
    dep_a = _deployment(account_id="acc-pnl")
    dep_b = _deployment(account_id="acc-pnl")
    pnls_flat = {str(dep_a.id): 0.0, str(dep_b.id): 0.0}
    observe_account_loss_breaker_equity(
        "acc-pnl", 100_000.0, now=DAY1,
        deployments=[dep_a, dep_b], deployment_pnls=pnls_flat, env_values=NO_ENV,
    )
    # Account equity -3% (A -6k, B +3k): under the 5% account daily limit.
    observe_account_loss_breaker_equity(
        "acc-pnl", 97_000.0, now=DAY1 + timedelta(minutes=1),
        deployments=[dep_a, dep_b],
        deployment_pnls={str(dep_a.id): -6_000.0, str(dep_b.id): 3_000.0},
        env_values=NO_ENV,
    )
    account_state = get_live_loss_breaker_state(account_loss_breaker_scope("acc-pnl"))
    assert account_state.daily_breached is False
    # A's synthetic equity: 97_000 - (-3_000) + (-6_000) = 94_000 -> -6%: trips.
    state_a = get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep_a.id)))
    assert state_a.daily_breached is True
    assert state_a.last_equity == pytest.approx(94_000.0)
    # B's synthetic equity: 97_000 - (-3_000) + 3_000 = 103_000 -> +3%: fine.
    state_b = get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep_b.id)))
    assert state_b.daily_breached is False
    assert state_b.drawdown_breached is False


def test_gate_uses_deployment_equity_not_account_equity():
    dep = _deployment(account_id="acc-v")
    _observe("acc-v", 100_000.0, DAY1)
    # The deployment scope seeds its baseline from its OWN first observation:
    # flat synthetic equity first, then the -6% drop is measured against it.
    _gate(dep, equity=100_000.0, now=DAY1, deployment_equity=100_000.0)
    # Account equity flat at 100k but this deployment's synthetic equity is
    # 94k (-6%): only the deployment scope trips.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            dep, equity=100_000.0, now=DAY1 + timedelta(minutes=1),
            deployment_equity=94_000.0,
        )
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(dep.id))


def test_gate_refuses_when_deployment_pnl_unmeasurable():
    dep = _deployment(account_id="acc-nopnl")
    _observe("acc-nopnl", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            dep, equity=100_000.0, deployment_equity=None,
            now=DAY1, env_values=NO_ENV,
        )
    assert excinfo.value.state_unreadable is True
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(dep.id))


def test_gate_skips_unmeasurable_deployment_scope_when_disabled():
    dep = _deployment(
        account_id="acc-nopnl2", settings={"loss_breaker_enabled": False}
    )
    _observe("acc-nopnl2", 100_000.0, DAY1)
    # Deployment breaker disabled: no P&L needed, account scope still passes.
    refuse_entry_if_loss_breaker_tripped(
        dep, equity=100_000.0, deployment_equity=None,
        now=DAY1, env_values=NO_ENV,
    )


def test_poll_skips_deployment_scopes_when_pnl_unmeasurable():
    dep = _deployment(account_id="acc-pnp")
    observe_account_loss_breaker_equity(
        "acc-pnp", 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls=None, env_values=NO_ENV,
    )
    # Account scope observed; the deployment scope was skipped entirely.
    assert get_live_loss_breaker_state(account_loss_breaker_scope("acc-pnp")) is not None
    assert (
        get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep.id)))
        is None
    )


# ─── Deployment P&L reconstruction from the fill ledger ────────────────────────


def _persist_fill(
    account_id: str,
    deployment_id: str,
    symbol: str,
    action: str,
    qty: float,
    price: float,
    *,
    observed: str | None = "observed",
    commission: float | None = None,
) -> None:
    """Persist account+deployment+signal+execution rows for the flow ledger."""
    import shared.database as database
    from shared.models import LiveExecution, LiveSignal

    database.init_db()
    with database.get_session() as session:
        if session.get(database.LiveAccountModel, account_id) is None:
            session.add(
                database.LiveAccountModel(id=account_id, broker="test", name=account_id)
            )
        if session.get(database.LiveDeploymentModel, deployment_id) is None:
            session.add(
                database.LiveDeploymentModel(
                    id=deployment_id,
                    strategy_name="t",
                    account_id=account_id,
                    symbols_json=[symbol],
                    timeframe="1h",
                )
            )
        session.commit()
    signal = LiveSignal(
        id=uuid4(),
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        theoretical_price=price,
        timestamp=DAY1,
        metadata={},
    )
    save_live_signal(signal)
    save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=price,
            filled_qty=qty,
            commission=commission,
            timestamp=DAY1,
            broker_order_id=f"bo-{uuid4()}",
            actual_price_observed=observed,
        )
    )


class _PnlAdapter:
    """Adapter stub for deployment-P&L marking: flat quotes, unit pip value."""

    def __init__(self, price: float = 100.0, *, pip: float = 1.0, quote_fails: bool = False):
        self._price = price
        self._pip = pip
        self._quote_fails = quote_fails

    async def get_quote(self, symbol):
        if self._quote_fails:
            raise RuntimeError("no quote")
        return LiveQuote(bid=self._price, ask=self._price, timestamp=datetime.now(timezone.utc))

    async def get_pip_value(self, symbol):
        from live_runner.base_adapter import LivePipValue

        return LivePipValue(
            value=self._pip, units_per_qty=1.0, conversion_rate=1.0,
            quote_asset="USD", account_asset="USD", source="test",
        )


def test_deployment_pnl_combines_realized_and_unrealized():
    """buy 100@100, sell 50@110, mark 115, pip 1 -> pnl 1250."""
    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-flow", dep_id, "AAPL", "buy", 100.0, 100.0)
    _persist_fill("acc-flow", dep_id, "AAPL", "sell", 50.0, 110.0)
    pnls = asyncio.run(account_deployment_trading_pnls(_PnlAdapter(115.0), "acc-flow"))
    assert pnls is not None
    assert pnls[dep_id] == pytest.approx(1_250.0)


def test_deployment_pnl_subtracts_commission():
    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-flow2", dep_id, "AAPL", "buy", 10.0, 100.0, commission=5.0)
    pnls = asyncio.run(account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-flow2"))
    assert pnls is not None
    # Flat mark: cash -1000 + qty 10 * 100 = 0, minus commission 5.
    assert pnls[dep_id] == pytest.approx(-5.0)


def test_deployment_pnl_fails_closed_on_untrusted_fill_price():
    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-flow3", dep_id, "AAPL", "buy", 10.0, 100.0, observed="absent")
    pnls = asyncio.run(account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-flow3"))
    assert pnls is None


def test_deployment_pnl_fails_closed_without_quote_for_open_position():
    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-flow4", dep_id, "AAPL", "buy", 10.0, 100.0)
    pnls = asyncio.run(
        account_deployment_trading_pnls(_PnlAdapter(quote_fails=True), "acc-flow4")
    )
    assert pnls is None


def test_deployment_pnl_empty_ledger_returns_empty_map():
    from live_runner.loss_breaker import account_deployment_trading_pnls

    pnls = asyncio.run(account_deployment_trading_pnls(_PnlAdapter(), "acc-no-fills"))
    assert pnls == {}


# ─── Daemon wiring: poll, claim path, run_daemon (#301 review mutants) ─────────


class _PollAdapter:
    """Balance-poll adapter: scripted equity, no positions, unit pip value."""

    def __init__(self, equities: list[float]) -> None:
        self._equities = list(equities)

    async def get_balance(self):
        equity = self._equities.pop(0) if len(self._equities) > 1 else self._equities[0]
        return equity, equity

    async def get_quote(self, symbol):
        return LiveQuote(bid=100.0, ask=100.0, timestamp=datetime.now(timezone.utc))

    async def get_pip_value(self, symbol):
        from live_runner.base_adapter import LivePipValue

        return LivePipValue(
            value=1.0, units_per_qty=1.0, conversion_rate=1.0,
            quote_asset="USD", account_asset="USD", source="test",
        )


def test_poll_adapter_balance_latches_account_and_deployment_scopes():
    """Drives the real ``_poll_adapter_balance``: a 6% equity drop latches
    BOTH the account scope and the deployment scope on the poll itself, and
    the deployment row seeds its baseline from its OWN first observation
    (created mid-stream, never inheriting the account baseline)."""
    dep = _deployment(account_id="acc-pollwire")
    adapter = _PollAdapter([100_000.0, 94_000.0])
    for equity_time in (DAY1, DAY1 + timedelta(minutes=1)):
        asyncio.run(
            daemon._poll_adapter_balance(
                {"acc-pollwire": adapter},
                "acc-pollwire",
                adapter,
                equity_time,
                current_deployments=lambda: [dep],
            )
        )
    account_state = get_live_loss_breaker_state(account_loss_breaker_scope("acc-pollwire"))
    assert account_state.daily_breached is True
    dep_state = get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep.id)))
    assert dep_state is not None
    assert dep_state.daily_breached is True
    # The deployment scope seeds its baseline from its OWN first observation
    # (here V == account equity because no other deployment has P&L) - never
    # from the account row's baseline.
    assert dep_state.day_start_equity == pytest.approx(100_000.0)


def test_poll_balances_forwards_current_deployments():
    """``_poll_balances`` must hand ``current_deployments`` to the per-account
    poll - without it deployment scopes can never be observed."""
    captured = {}

    async def _fake_poll_adapter_balance(*args, **kwargs):
        captured["current_deployments"] = kwargs.get("current_deployments")

    import live_runner.daemon as daemon_module

    original = daemon_module._poll_adapter_balance
    daemon_module._poll_adapter_balance = _fake_poll_adapter_balance
    dep = _deployment(account_id="acc-fwd")
    try:
        async def _one_tick():
            import contextlib

            task = asyncio.create_task(
                daemon_module._poll_balances(
                    {"acc-fwd": _PollAdapter([1.0])},
                    interval_seconds=60.0,
                    current_deployments=lambda: [dep],
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_one_tick())
    finally:
        daemon_module._poll_adapter_balance = original
    assert captured["current_deployments"] is not None
    assert captured["current_deployments"]()[0].id == dep.id


def test_start_balance_poller_wires_deployment_snapshot():
    """``run_daemon`` starts the poller through ``_start_balance_poller``; the
    snapshot must reach ``_poll_balances`` as ``current_deployments``."""
    import live_runner.daemon as daemon_module

    captured = {}

    async def _fake_poll_balances(adapters, **kwargs):
        captured.update(kwargs)

    original = daemon_module._poll_balances
    daemon_module._poll_balances = _fake_poll_balances
    dep = _deployment(account_id="acc-wire")
    try:
        async def _run():
            task = daemon_module._start_balance_poller({}, [dep])
            await task

        asyncio.run(_run())
    finally:
        daemon_module._poll_balances = original
    assert callable(captured.get("current_deployments"))
    assert captured["current_deployments"]()[0].id == dep.id


def test_claim_path_logs_breaker_refusal_and_submits_nothing(monkeypatch, caplog):
    """A LossBreakerBreachError in the claim path logs the distinct breaker
    warning and returns BEFORE the submission claim - nothing is sent and the
    signal is not marked failed by the generic handler."""
    import logging

    from risk.models import TradeSignal

    dep = _deployment(account_id="acc-claim")
    adapter = _Adapter(equity=100_000.0)
    claims = []

    async def _raise_breaker(*args, **kwargs):
        raise LossBreakerBreachError(
            "refused",
            scope=account_loss_breaker_scope("acc-claim"),
            kind="daily",
            account_id="acc-claim",
            deployment_id=str(dep.id),
        )

    def _claim(*args, **kwargs):
        claims.append(args)
        return True

    monkeypatch.setattr(daemon, "prepare_order_with_risk", _raise_breaker)
    monkeypatch.setattr(daemon, "claim_live_deployment_submission", _claim)
    normalized = TradeSignal(
        action="buy", symbol="AAPL", entry_price=100.0, stop_loss=95.0,
        take_profit=110.0, timeframe="1h", metadata={},
    )
    live_signal = _entry_signal(dep)
    logger = daemon.logger
    with caplog.at_level(logging.WARNING, logger=logger.name):
        asyncio.run(
            daemon._prepare_claim_and_submit_signal(
                "acc-claim", adapter, dep, normalized, live_signal, is_exit=False
            )
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any("loss circuit breaker" in m for m in messages)
    breaker_records = [
        r for r in caplog.records
        if getattr(r, "data", {}).get("breaker_scope")
    ]
    assert breaker_records, "expected the distinct breaker refusal record"
    assert claims == [], "submission claim must not run for a breaker refusal"


def test_drawdown_trips_at_exactly_twenty_percent():
    """Boundary pin: drawdown == threshold trips (>=, not >)."""
    dep = _deployment(account_id="acc-dd-edge")
    _observe("acc-dd-edge", 100_000.0, DAY1)
    _observe("acc-dd-edge", 125_000.0, DAY1 + timedelta(hours=1))  # peak
    # Same UTC day: day-start stays 100k, so only the drawdown leg can trip.
    # (125k - 100k) / 125k = exactly 20%.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(dep, equity=100_000.0, now=DAY1 + timedelta(hours=2), env_values=NO_ENV)
    assert excinfo.value.kind == "drawdown"
    state = get_live_loss_breaker_state(account_loss_breaker_scope("acc-dd-edge"))
    assert state.drawdown_breached is True


def test_breaker_records_broker_equity_not_account_balance_override():
    """``account_balance`` is a sizing input; the breaker must observe the
    adapter's equity so a caller-supplied number cannot poison the series."""
    account_id = "acc-override"
    dep = _deployment(account_id=account_id)
    _observe(account_id, 100_000.0, DAY1)
    adapter = _Adapter(equity=90_000.0)  # broker equity -10%: breaker must trip
    with pytest.raises(LossBreakerBreachError):
        asyncio.run(
            daemon.prepare_order_with_risk(
                adapter, dep, _entry_signal(dep), account_balance=1_000_000.0
            )
        )


# ─── Bundle-32 review fixes (#301) ────────────────────────────────────────────


def test_new_deployment_scope_seeds_from_own_equity_not_account_baseline():
    """The bundle-32 probe: D1 has +7 000 lifetime P&L, account equity 107 000.

    A brand-new D2 must seed day_start/peak from its OWN synthetic equity
    V = E - others' P&L = 100 000 - not from the account row's 107 000, which
    would trip its daily breaker at once. The reverse direction (others at a
    loss) must not hide D2's first-day losses either.
    """
    dep_old = _deployment(account_id="acc-seed")
    dep_new = _deployment(account_id="acc-seed")
    old_id, new_id = str(dep_old.id), str(dep_new.id)
    deployments = [dep_old, dep_new]
    pnls_profit = {old_id: 7_000.0, new_id: 0.0}
    # Poll 1: account equity 107k, D1 up 7k -> D2's V = 107k - 7k = 100k.
    observe_account_loss_breaker_equity(
        "acc-seed", 107_000.0, now=DAY1,
        deployments=deployments, deployment_pnls=pnls_profit, env_values=NO_ENV,
    )
    state_new = get_live_loss_breaker_state(
        deployment_loss_breaker_scope(new_id)
    )
    assert state_new.day_start_equity == pytest.approx(100_000.0)
    assert state_new.peak_equity == pytest.approx(100_000.0)
    assert state_new.daily_breached is False
    assert state_new.drawdown_breached is False
    # Poll 2, same equity: nothing trips.
    observe_account_loss_breaker_equity(
        "acc-seed", 107_000.0, now=DAY1 + timedelta(minutes=1),
        deployments=deployments, deployment_pnls=pnls_profit, env_values=NO_ENV,
    )
    state_new = get_live_loss_breaker_state(
        deployment_loss_breaker_scope(new_id)
    )
    assert state_new.daily_breached is False


def test_new_deployment_scope_does_not_hide_own_losses_behind_others():
    """Reverse direction: D1 at a loss must not push D2's baseline below its
    own V (the old account-seed made V > baseline, hiding D2's first losses)."""
    dep_old = _deployment(account_id="acc-seed2")
    dep_new = _deployment(account_id="acc-seed2")
    old_id, new_id = str(dep_old.id), str(dep_new.id)
    deployments = [dep_old, dep_new]
    pnls_loss = {old_id: -10_000.0, new_id: 0.0}
    # Account equity 90k with D1 -10k -> D2's V = 90k - (-10k) = 100k.
    observe_account_loss_breaker_equity(
        "acc-seed2", 90_000.0, now=DAY1,
        deployments=deployments, deployment_pnls=pnls_loss, env_values=NO_ENV,
    )
    state_new = get_live_loss_breaker_state(
        deployment_loss_breaker_scope(new_id)
    )
    assert state_new.day_start_equity == pytest.approx(100_000.0)
    # Consistent account equity: E = K + total P&L = 100k + (-22k) = 78k.
    # D2's V = E - others' P&L = 78k - (-16k) = 94k -> -6% vs its own 100k
    # baseline: the daily breaker trips on D2's OWN loss even though the
    # account equity move (-12k) is mostly D1's.
    observe_account_loss_breaker_equity(
        "acc-seed2", 78_000.0, now=DAY1 + timedelta(minutes=1),
        deployments=deployments,
        deployment_pnls={old_id: -16_000.0, new_id: -6_000.0},
        env_values=NO_ENV,
    )
    state_new = get_live_loss_breaker_state(
        deployment_loss_breaker_scope(new_id)
    )
    assert state_new.daily_breached is True


def test_unmeasurable_pnl_alerts_once_per_episode_and_names_fills(
    caplog,
):
    """An untrusted-price fill fails closed AND raises one ERROR
    ``loss_breaker_pnl_unmeasurable`` per episode naming the fill - not a
    silent WARNING on every poll."""
    import logging

    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-unpriced", dep_id, "AAPL", "buy", 10.0, 100.0, observed="absent")
    logger = loss_breaker.logger
    with caplog.at_level(logging.ERROR, logger=logger.name):
        for _ in range(3):
            pnls = asyncio.run(
                account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-unpriced")
            )
            assert pnls is None
    alerts = [
        r for r in caplog.records
        if getattr(r, "data", {}).get("alert") == "loss_breaker_pnl_unmeasurable"
    ]
    assert len(alerts) == 1
    assert alerts[0].data["reason"] == "unpriced_fills"
    assert alerts[0].data["unpriced_fill_count"] == 1
    named = alerts[0].data["untrusted_fills"]
    assert named and named[0]["symbol"] == "AAPL"
    assert named[0]["actual_price_observed"] == "absent"


def test_unmeasurable_pnl_alert_clears_when_measurable_again(caplog):
    import logging

    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-unpriced2", dep_id, "AAPL", "buy", 10.0, 100.0, observed="absent")
    logger = loss_breaker.logger
    with caplog.at_level(logging.ERROR, logger=logger.name):
        assert (
            asyncio.run(
                account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-unpriced2")
            )
            is None
        )
    # Repair the price verdict, then measure: the episode ends and a NEW
    # unmeasurable episode would alert again.
    from sqlalchemy import update

    import shared.database as database

    with database.get_session() as session:
        session.exec(
            update(database.LiveExecutionModel)
            .where(database.LiveExecutionModel.actual_price_observed == "absent")
            .values(actual_price_observed="observed")
        )
        session.commit()
    pnls = asyncio.run(
        account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-unpriced2")
    )
    assert pnls is not None
    assert ("acc-unpriced2", "unpriced_fills") not in loss_breaker._PNL_UNMEASURABLE_ALERTED


def test_ledger_reconciliation_flag_fails_deployment_pnl_closed(caplog):
    """An unresolved ``ledger_reconciliation_required`` audit row on the
    account means the fill ledger may be missing closes (#277/#330): the
    deployment P&L is untrustworthy, so it fails closed with an alert."""
    import logging

    from live_runner.loss_breaker import account_deployment_trading_pnls
    from shared.database import record_live_control_audit

    dep_id = str(uuid4())
    _persist_fill("acc-recon", dep_id, "AAPL", "buy", 10.0, 100.0)
    record_live_control_audit(
        action="ledger_reconciliation_required",
        target_type="account_symbol",
        target_id="acc-recon:AAPL",
        actor="test",
        detail={"cause": "unattributed close"},
    )
    logger = loss_breaker.logger
    with caplog.at_level(logging.ERROR, logger=logger.name):
        pnls = asyncio.run(
            account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-recon")
        )
    assert pnls is None
    alerts = [
        r for r in caplog.records
        if getattr(r, "data", {}).get("alert") == "loss_breaker_pnl_unmeasurable"
    ]
    assert alerts and alerts[0].data["reason"] == "ledger_reconciliation"


def test_ledger_reconciliation_flag_does_not_leak_across_account_prefix():
    """``acc-1``'s flag must not fail ``acc-10``'s scopes closed."""
    from shared.database import account_has_ledger_reconciliation_flags, record_live_control_audit

    record_live_control_audit(
        action="ledger_reconciliation_required",
        target_type="account_symbol",
        target_id="acc-1:AAPL",
        actor="test",
        detail={},
    )
    assert account_has_ledger_reconciliation_flags("acc-1") is True
    assert account_has_ledger_reconciliation_flags("acc-10") is False
    assert account_has_ledger_reconciliation_flags("") is False


def test_deployment_pnl_marks_long_at_bid_and_short_at_ask():
    """The conservative side of the book: a long marks at bid, a short at ask.
    With bid != ask the two marks differ - pinning gross-vs-net and the side."""
    from live_runner.loss_breaker import account_deployment_trading_pnls

    class _SpreadAdapter(_PnlAdapter):
        async def get_quote(self, symbol):
            return LiveQuote(bid=99.0, ask=101.0, timestamp=datetime.now(timezone.utc))

    dep_id = str(uuid4())
    # Long 10 @ 100, mark bid 99 -> pnl = -1000 + 10*99 = -10.
    _persist_fill("acc-spread", dep_id, "AAPL", "buy", 10.0, 100.0)
    pnls = asyncio.run(account_deployment_trading_pnls(_SpreadAdapter(), "acc-spread"))
    assert pnls is not None
    assert pnls[dep_id] == pytest.approx(-10.0)

    # Short 10 @ 100 (sell fill), mark ask 101 -> cash +1000, net_qty -10:
    # pnl = 1000 + (-10)*101 = -10.
    dep2 = str(uuid4())
    _persist_fill("acc-spread2", dep2, "AAPL", "sell", 10.0, 100.0)
    pnls = asyncio.run(account_deployment_trading_pnls(_SpreadAdapter(), "acc-spread2"))
    assert pnls is not None
    assert pnls[dep2] == pytest.approx(-10.0)


def test_deployment_pnl_sums_positions_net_not_gross():
    """Two positions on one deployment+symbol: buy 10@100, sell 4@110, mark 100.

    Net: cash -1000+440=-560, net_qty 6 -> pnl = -560 + 600 = 40.
    Gross (wrong): would treat the sell as closing nothing -> -1000+1000=0 plus
    the sell proceeds counted differently; the net figure is what the ledger
    means. Also covers a second symbol to pin the per-symbol aggregation.
    """
    from live_runner.loss_breaker import account_deployment_trading_pnls

    dep_id = str(uuid4())
    _persist_fill("acc-net", dep_id, "AAPL", "buy", 10.0, 100.0)
    _persist_fill("acc-net", dep_id, "AAPL", "sell", 4.0, 110.0)
    _persist_fill("acc-net", dep_id, "MSFT", "buy", 5.0, 200.0)
    pnls = asyncio.run(account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-net"))
    assert pnls is not None
    # AAPL: -560 + 6*100 = 40. MSFT: -1000 + 5*100(pip=1, mark=100) = -500.
    # _PnlAdapter marks every symbol at 100.0.
    assert pnls[dep_id] == pytest.approx(40.0 - 500.0)


def test_prepare_order_refuses_losing_deployment_at_dispatch():
    """End-to-end dispatch pin (#14 mutant): the deployment scope's synthetic
    equity - breaker_equity - total_pnl + own_pnl - is what the gate sees.

    dep_a -25k and dep_b +25k net to zero account-equity move, so ONLY the
    deployment scope can trip: dep_a's V = 100k - 0 + (-25k) = 75k (-25%
    drawdown vs its 100k peak - the entry gate runs on the real clock, so the
    timeless drawdown latch is what fires, not the UTC-day daily breaker),
    dep_b's V = 125k (+25%).
    """
    account_id = "acc-dispatch"
    dep_a = _deployment(account_id=account_id)
    dep_b = _deployment(account_id=account_id)
    a_id, b_id = str(dep_a.id), str(dep_b.id)
    deployments = [dep_a, dep_b]
    # First poll: flat P&L -> both deployment scopes baseline at V = 100k.
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=deployments,
        deployment_pnls={a_id: 0.0, b_id: 0.0},
        env_values=NO_ENV,
    )
    adapter = _Adapter(equity=100_000.0)

    async def _pnls(adapter_arg, acct):
        return {a_id: -25_000.0, b_id: 25_000.0}

    import live_runner.daemon as daemon_module

    original = daemon_module.account_deployment_trading_pnls
    daemon_module.account_deployment_trading_pnls = _pnls
    try:
        with pytest.raises(LossBreakerBreachError) as excinfo:
            asyncio.run(
                daemon.prepare_order_with_risk(
                    adapter, dep_a, _entry_signal(dep_a)
                )
            )
        assert excinfo.value.scope == deployment_loss_breaker_scope(a_id)
        # dep B's V = 100k - 0 + 25k = 125k -> +25%: allowed.
        _prepared, qty = asyncio.run(
            daemon.prepare_order_with_risk(adapter, dep_b, _entry_signal(dep_b))
        )
        assert qty > 0
    finally:
        daemon_module.account_deployment_trading_pnls = original


def test_prepare_order_uses_get_equity_not_the_sizing_balance():
    """The breaker reads ``adapter.get_equity`` - the separate equity path -
    never ``get_balance``'s equity slot that sizing uses (#267)."""
    account_id = "acc-eqpath"
    dep = _deployment(account_id=account_id)
    _observe(account_id, 100_000.0, DAY1)

    class _SplitAdapter(_Adapter):
        async def get_balance(self):
            # Sizing sees realized balance; equity slot = balance here.
            return 100_000.0, 100_000.0

        async def get_equity(self):
            # True equity incl. unrealized: -30% drawdown vs the 100k peak
            # trips on the real clock (the entry gate takes no ``now``).
            return 70_000.0

    adapter = _SplitAdapter(equity=100_000.0)
    with pytest.raises(LossBreakerBreachError):
        asyncio.run(
            daemon.prepare_order_with_risk(adapter, dep, _entry_signal(dep))
        )


def test_prepare_order_refuses_on_malformed_loss_breaker_config():
    """A bad ``loss_breaker_*`` deployment setting is a distinct
    configuration refusal (``config_invalid``), not a generic error."""
    account_id = "acc-badcfg"
    dep_bad = _deployment(
        account_id=account_id,
        settings={"loss_breaker_daily_loss_fraction": "bogus"},
    )
    _observe(account_id, 100_000.0, DAY1)
    adapter = _Adapter(equity=100_000.0)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        asyncio.run(
            daemon.prepare_order_with_risk(adapter, dep_bad, _entry_signal(dep_bad))
        )
    assert excinfo.value.kind == "config_invalid"
    assert "invalid" in str(excinfo.value).lower()


def test_gate_refuses_on_malformed_env_config():
    dep = _deployment(account_id="acc-badcfg2")
    _observe("acc-badcfg2", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            dep, equity=100_000.0, now=DAY1,
            env_values={"LOSS_BREAKER_DAILY_LOSS_FRACTION": "bogus"},
        )
    assert excinfo.value.kind == "config_invalid"
    assert excinfo.value.state_unreadable is True


def test_poll_observes_equity_not_balance():
    """#18 pin: the poll hands the breaker's equity (get_balance()[1] via the
    fallback path) to the observation - a (balance, equity) split must record
    equity, not balance."""
    account_id = "acc-eqvsbal"
    dep = _deployment(account_id=account_id)

    class _SplitPollAdapter(_PollAdapter):
        async def get_balance(self):
            return 100_000.0, 94_000.0

    adapter = _SplitPollAdapter([1.0])
    asyncio.run(
        daemon._poll_adapter_balance(
            {account_id: adapter},
            account_id,
            adapter,
            DAY1,
            current_deployments=lambda: [dep],
        )
    )
    state = get_live_loss_breaker_state(account_loss_breaker_scope(account_id))
    assert state.last_equity == pytest.approx(94_000.0)
    # The saved balance snapshot keeps its own (balance, equity) record.
    latest = get_latest_live_balance(account_id)
    assert latest.balance == pytest.approx(100_000.0)
    assert latest.equity == pytest.approx(94_000.0)


def test_zero_equity_poll_writes_no_baseline_row():
    """#5 pin: a zero/negative equity observation must not write a zero
    baseline that would later read as a breach or poison the series."""
    observe_account_loss_breaker_equity(
        "acc-zero", 0.0, now=DAY1, deployments=[], deployment_pnls={}, env_values=NO_ENV
    )
    observe_account_loss_breaker_equity(
        "acc-zero", -50.0, now=DAY1, deployments=[], deployment_pnls={}, env_values=NO_ENV
    )
    assert list_live_loss_breaker_states("acc-zero") == []


def test_pip_values_are_cached_per_symbol_and_day():
    """The poll resolves pip values for every symbol the account ever traded;
    a cached value must not be re-fetched within the same UTC day."""
    from live_runner.loss_breaker import account_deployment_trading_pnls

    calls = []

    class _CountingAdapter(_PnlAdapter):
        async def get_pip_value(self, symbol):
            calls.append(symbol)
            return await super().get_pip_value(symbol)

    dep_id = str(uuid4())
    _persist_fill("acc-pipcache", dep_id, "AAPL", "buy", 10.0, 100.0)
    adapter = _CountingAdapter(100.0)
    first = asyncio.run(account_deployment_trading_pnls(adapter, "acc-pipcache"))
    second = asyncio.run(account_deployment_trading_pnls(adapter, "acc-pipcache"))
    assert first is not None and second is not None
    assert calls == ["AAPL"]


def test_accounts_env_file_override_is_honoured(tmp_path):
    """``set_accounts_env_file`` points LOSS_BREAKER_* resolution at the
    daemon's configured accounts file (the --accounts-env fix)."""
    env_file = tmp_path / "custom.accounts.env"
    env_file.write_text("LOSS_BREAKER_DAILY_LOSS_FRACTION=0.07\n")
    loss_breaker.set_accounts_env_file(env_file)
    try:
        limits = account_loss_breaker_limits("acc-x", env_values=None)
        assert limits.daily_loss_fraction == pytest.approx(0.07)
    finally:
        loss_breaker.set_accounts_env_file(None)
    limits = account_loss_breaker_limits("acc-x", env_values=None)
    assert limits.daily_loss_fraction == pytest.approx(0.05)


# ─── Bundle-34 review fixes ───────────────────────────────────────────────────


def test_pip_values_are_cached_per_account_not_shared():
    """A pip value is in the ACCOUNT's currency: two accounts on the same
    symbol and day must each resolve their own, and a second lookup on the
    SAME account still hits the cache."""
    from live_runner.loss_breaker import account_deployment_trading_pnls

    class _CountingAdapter(_PnlAdapter):
        def __init__(self, pip: float):
            super().__init__(101.0, pip=pip)
            self.pip_calls = 0

        async def get_pip_value(self, symbol):
            self.pip_calls += 1
            return await super().get_pip_value(symbol)

    dep_a, dep_b = str(uuid4()), str(uuid4())
    _persist_fill("acc-pip-a", dep_a, "AAPL", "buy", 10.0, 100.0)
    _persist_fill("acc-pip-b", dep_b, "AAPL", "buy", 10.0, 100.0)
    adapter_a, adapter_b = _CountingAdapter(1.0), _CountingAdapter(2.0)
    pnls_a = asyncio.run(account_deployment_trading_pnls(adapter_a, "acc-pip-a"))
    pnls_b = asyncio.run(account_deployment_trading_pnls(adapter_b, "acc-pip-b"))
    assert pnls_a is not None and pnls_b is not None
    # buy 10@100 marked at 101 -> pip * ( -1000 + 1010 ) = pip * 10.
    assert pnls_a[dep_a] == pytest.approx(10.0)
    assert pnls_b[dep_b] == pytest.approx(20.0)
    assert adapter_a.pip_calls == 1 and adapter_b.pip_calls == 1
    # Same account + symbol + day: cached, no second fetch.
    again = asyncio.run(account_deployment_trading_pnls(adapter_a, "acc-pip-a"))
    assert again is not None
    assert adapter_a.pip_calls == 1


def test_poll_observes_get_equity_not_balance_tuple():
    """cTrader shape: ``get_balance`` reports realized balance in BOTH slots
    while ``get_equity`` adds unrealized P&L. The breaker must record 94k,
    not 100k - a ``get_equity = None`` mutant would observe 100k."""
    account_id = "acc-eqpath-poll"
    dep = _deployment(account_id=account_id)

    class _CtraderLikePollAdapter(_PollAdapter):
        async def get_balance(self):
            return 100_000.0, 100_000.0

        async def get_equity(self):
            return 94_000.0

    adapter = _CtraderLikePollAdapter([1.0])
    asyncio.run(
        daemon._poll_adapter_balance(
            {account_id: adapter},
            account_id,
            adapter,
            DAY1,
            current_deployments=lambda: [dep],
        )
    )
    state = get_live_loss_breaker_state(account_loss_breaker_scope(account_id))
    assert state is not None
    assert state.last_equity == pytest.approx(94_000.0)


def test_base_adapter_get_equity_defaults_to_balance_equity_slot():
    """The ``BaseLiveAdapter.get_equity`` default is ``get_balance()[1]`` -
    adapters whose equity slot already carries true equity need no override."""
    from live_runner.base_adapter import BaseLiveAdapter

    class _MinimalAdapter(BaseLiveAdapter):
        async def connect(self):
            pass

        async def submit_order(self, signal, *, qty=None):
            return "order-1"

        async def listen_executions(self):
            pass

        async def get_balance(self):
            return 100.0, 90.0

    assert asyncio.run(_MinimalAdapter().get_equity()) == pytest.approx(90.0)


def test_ledger_flag_resolution_unblocks_and_newer_flag_reblocks():
    """A ``ledger_reconciliation_required`` flag fails deployment P&L closed
    only until an operator resolution; a flag NEWER than the latest
    resolution blocks again."""
    from live_runner.loss_breaker import account_deployment_trading_pnls
    from shared.database import (
        account_has_ledger_reconciliation_flags,
        list_unresolved_ledger_reconciliation_flags,
        record_live_control_audit,
        resolve_ledger_reconciliation_flag,
    )

    dep_id = str(uuid4())
    _persist_fill("acc-resolve", dep_id, "AAPL", "buy", 10.0, 100.0)
    record_live_control_audit(
        action="ledger_reconciliation_required",
        target_type="account_symbol",
        target_id="acc-resolve:AAPL",
        actor="test",
        detail={"cause": "unattributed close"},
    )
    assert account_has_ledger_reconciliation_flags("acc-resolve") is True
    assert list_unresolved_ledger_reconciliation_flags("acc-resolve") == [
        "acc-resolve:AAPL"
    ]
    assert (
        asyncio.run(account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-resolve"))
        is None
    )

    resolve_ledger_reconciliation_flag(
        account_id="acc-resolve",
        symbol="aapl",
        actor="operator",
        note="reconciled against broker statement",
    )
    assert account_has_ledger_reconciliation_flags("acc-resolve") is False
    assert list_unresolved_ledger_reconciliation_flags("acc-resolve") == []
    pnls = asyncio.run(account_deployment_trading_pnls(_PnlAdapter(100.0), "acc-resolve"))
    assert pnls is not None and pnls[dep_id] == pytest.approx(0.0)

    # A flag written AFTER the resolution is newer: it blocks again.
    record_live_control_audit(
        action="ledger_reconciliation_required",
        target_type="account_symbol",
        target_id="acc-resolve:AAPL",
        actor="test",
        detail={"cause": "second unattributed close"},
    )
    assert account_has_ledger_reconciliation_flags("acc-resolve") is True
    assert list_unresolved_ledger_reconciliation_flags("acc-resolve") == [
        "acc-resolve:AAPL"
    ]


def test_ledger_flag_resolution_is_per_symbol():
    """Resolving ``acc-multi:MSFT`` must not clear an unresolved ``AAPL`` flag."""
    from shared.database import (
        list_unresolved_ledger_reconciliation_flags,
        record_live_control_audit,
        resolve_ledger_reconciliation_flag,
    )

    for symbol in ("AAPL", "MSFT"):
        record_live_control_audit(
            action="ledger_reconciliation_required",
            target_type="account_symbol",
            target_id=f"acc-multi:{symbol}",
            actor="test",
            detail={},
        )
    resolve_ledger_reconciliation_flag(
        account_id="acc-multi", symbol="MSFT", actor="operator", note="done"
    )
    assert list_unresolved_ledger_reconciliation_flags("acc-multi") == [
        "acc-multi:AAPL"
    ]


def test_ledger_flag_on_acc_10_does_not_flag_acc_1():
    """LIKE-leak pin in the other direction: ``acc-10``'s flag must not fail
    ``acc-1``'s scopes closed."""
    from shared.database import (
        account_has_ledger_reconciliation_flags,
        record_live_control_audit,
    )

    record_live_control_audit(
        action="ledger_reconciliation_required",
        target_type="account_symbol",
        target_id="acc-10:AAPL",
        actor="test",
        detail={},
    )
    assert account_has_ledger_reconciliation_flags("acc-10") is True
    assert account_has_ledger_reconciliation_flags("acc-1") is False


def test_poll_observes_account_scope_before_pnl_computation(monkeypatch):
    """The account scope must be observed even when deployment-P&L marking
    fails or is slow: cTrader quote lookups can eat the whole poll timeout,
    and observing afterwards would starve the between-entries alert."""
    account_id = "acc-pollorder"
    dep = _deployment(account_id=account_id)
    adapter = _PollAdapter([100_000.0])

    async def _boom(adapter_arg, acct):
        raise RuntimeError("pnl marking failed")

    monkeypatch.setattr(daemon, "account_deployment_trading_pnls", _boom)
    asyncio.run(
        daemon._poll_adapter_balance(
            {account_id: adapter},
            account_id,
            adapter,
            DAY1,
            current_deployments=lambda: [dep],
        )
    )
    state = get_live_loss_breaker_state(account_loss_breaker_scope(account_id))
    assert state is not None
    assert state.last_equity == pytest.approx(100_000.0)
    # The deployment scope was skipped (P&L unmeasurable) - not poisoned.
    assert (
        get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep.id)))
        is None
    )


def test_prepare_order_skips_equity_read_when_breakers_disabled(monkeypatch):
    """Both scopes disabled -> the breaker measures nothing, so dispatch must
    not pay for a ``get_equity`` read (a cTrader unrealized-P&L request) or a
    deployment-P&L lookup, and must not refuse on an unreadable equity."""
    account_id = "acc-brkoff"
    dep = _deployment(account_id=account_id)

    class _NoEquityAdapter(_Adapter):
        async def get_equity(self):
            raise AssertionError("get_equity must not be called")

    monkeypatch.setenv("LOSS_BREAKER_ENABLED", "false")
    adapter = _NoEquityAdapter(equity=100_000.0)
    _prepared, qty = asyncio.run(
        daemon.prepare_order_with_risk(adapter, dep, _entry_signal(dep))
    )
    assert qty > 0
    assert list_live_loss_breaker_states(account_id) == []


def test_gate_zero_equity_writes_no_state_row():
    """A refused zero-equity entry must not leave a baseline row behind."""
    dep = _deployment(account_id="acc-gatezero")
    with pytest.raises(LossBreakerBreachError):
        _gate(dep, equity=0.0, now=DAY1)
    assert get_live_loss_breaker_state(account_loss_breaker_scope("acc-gatezero")) is None
    assert (
        get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep.id)))
        is None
    )


def test_run_daemon_points_loss_breaker_env_at_accounts_env(monkeypatch, tmp_path):
    """``run_daemon`` must hand its ``--accounts-env`` file to the loss
    breaker so per-account LOSS_BREAKER_* values are honoured."""
    captured = {}
    monkeypatch.setattr(
        daemon,
        "set_accounts_env_file",
        lambda path: captured.setdefault("path", path),
    )

    def _noop_sync(*args, **kwargs):
        return None

    async def _noop_async(*args, **kwargs):
        return None

    class _Scheduler:
        async def start(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr(daemon, "_discover_and_register_discord_services", _noop_sync)
    monkeypatch.setattr(daemon, "_recover_failed_services", _noop_sync)
    monkeypatch.setattr(daemon, "ServiceScheduler", lambda: _Scheduler())
    monkeypatch.setattr(daemon, "sync_deployments_loop", _noop_async)
    monkeypatch.setattr(daemon, "sync_services_loop", _noop_async)
    monkeypatch.setattr(daemon, "run_trailing_stops_loop", _noop_async)
    monkeypatch.setattr(daemon, "_backfill_commissions_loop", _noop_async)
    monkeypatch.setattr(
        daemon,
        "_start_balance_poller",
        lambda *a, **k: asyncio.ensure_future(_noop_async()),
    )
    env_file = tmp_path / "custom.accounts.env"
    asyncio.run(daemon.run_daemon(accounts_env=env_file))
    assert captured["path"] == env_file
