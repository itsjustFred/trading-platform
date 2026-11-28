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
    reanchor_deployment_loss_breaker_scope,
    refuse_entry_if_loss_breaker_tripped,
)
from shared.database import (
    get_latest_live_balance,
    get_live_loss_breaker_state,
    list_live_loss_breaker_states,
    observe_live_loss_breaker,
    reanchor_live_loss_breaker_deployment,
    reset_live_loss_breaker_drawdown,
    save_live_balance,
    save_live_execution,
    save_live_signal,
    update_live_deployment_account,
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
    deployment_pnls: dict | None = None,
) -> None:
    """The entry gate with ``deployment_pnls`` defaulting to ``{}``.

    These tests have no fill-ledger P&L, so the measurable map is empty -
    the deployment scope's anchored equity then stays flat (its own P&L is
    zero) while the account scope tracks raw equity. Tests that exercise a
    deployment P&L pass ``deployment_pnls`` or call
    ``refuse_entry_if_loss_breaker_tripped`` directly.
    """
    return refuse_entry_if_loss_breaker_tripped(
        deployment,
        equity=equity,
        deployment_pnls={} if deployment_pnls is None else deployment_pnls,
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
    # (flat P&L -> anchored equity == account equity), then measures the
    # drop against it.
    _gate(tight, equity=100_000.0, now=DAY1, env_values=NO_ENV)
    # tight's own P&L is -2 000 while account equity is 98k: residual stays
    # 100k, so tight's anchored equity is 98k (-2%) - over its 1% daily
    # limit, under the account's 5%.
    pnls = {str(tight.id): -2_000.0, str(loose.id): 0.0}
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            tight, equity=98_000.0, now=DAY1 + timedelta(minutes=1),
            env_values=NO_ENV, deployment_pnls=pnls,
        )
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(tight.id))
    # The sibling deployment on the same account still trades: its own P&L
    # is flat so its anchored equity stays at its 100k baseline, and the
    # account scope is only -2 %.
    _gate(
        loose, equity=98_000.0, now=DAY1 + timedelta(minutes=1),
        env_values=NO_ENV, deployment_pnls=pnls,
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
    a_id, b_id = str(dep_a.id), str(dep_b.id)
    pnls_flat = {a_id: 0.0, b_id: 0.0}
    observe_account_loss_breaker_equity(
        "acc-poll",
        100_000.0,
        now=DAY1,
        deployments=[dep_a, dep_b],
        deployment_pnls=pnls_flat,
        env_values=NO_ENV,
    )
    # Second poll: each deployment's OWN P&L is -6k (account equity -12k,
    # residual unchanged at 100k) - both deployment scopes latch on the poll
    # itself, and so does the account scope.
    observe_account_loss_breaker_equity(
        "acc-poll",
        88_000.0,
        now=DAY1 + timedelta(minutes=1),
        deployments=[dep_a, dep_b],
        deployment_pnls={a_id: -6_000.0, b_id: -6_000.0},
        env_values=NO_ENV,
    )
    for dep in (dep_a, dep_b):
        with pytest.raises(LossBreakerBreachError):
            _gate(
                dep, equity=88_000.0, now=DAY1, env_values=NO_ENV,
                deployment_pnls={a_id: -6_000.0, b_id: -6_000.0},
            )
    state = get_live_loss_breaker_state(deployment_loss_breaker_scope(a_id))
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


def test_gate_uses_deployment_pnl_not_account_equity():
    dep = _deployment(account_id="acc-v")
    _observe("acc-v", 100_000.0, DAY1)
    # The deployment scope seeds its baseline from its OWN first observation:
    # flat P&L anchors it at 100k, then a -6k own-P&L move is measured
    # against it while account equity stays flat.
    _gate(dep, equity=100_000.0, now=DAY1, deployment_pnls={})
    # Account equity flat at 100k but this deployment's own P&L is -6k:
    # anchored equity 94k (-6%): only the deployment scope trips.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            dep, equity=100_000.0, now=DAY1 + timedelta(minutes=1),
            deployment_pnls={str(dep.id): -6_000.0},
        )
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(dep.id))


def test_gate_refuses_when_deployment_pnl_unmeasurable():
    dep = _deployment(account_id="acc-nopnl")
    _observe("acc-nopnl", 100_000.0, DAY1)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        refuse_entry_if_loss_breaker_tripped(
            dep, equity=100_000.0, deployment_pnls=None,
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
        dep, equity=100_000.0, deployment_pnls=None,
        now=DAY1, env_values=NO_ENV,
    )


def test_poll_skips_deployment_scopes_when_pnl_unmeasurable(caplog):
    dep = _deployment(account_id="acc-pnp")
    with caplog.at_level(logging.WARNING, logger="live-runner"):
        observe_account_loss_breaker_equity(
            "acc-pnp", 100_000.0, now=DAY1,
            deployments=[dep], deployment_pnls=None, env_values=NO_ENV,
        )
    # Account scope observed; the deployment scope was skipped entirely -
    # gracefully, with the skip warning and WITHOUT the generic
    # "observation failed" error a crash inside the loop would log.
    assert get_live_loss_breaker_state(account_loss_breaker_scope("acc-pnp")) is not None
    assert (
        get_live_loss_breaker_state(deployment_loss_breaker_scope(str(dep.id)))
        is None
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("scopes skipped this poll" in m for m in messages)
    assert not any("observation failed" in m for m in messages)


# ─── Deployment P&L reconstruction from the fill ledger ────────────────────────


def _ensure_live_account(account_id: str) -> None:
    """Persist the ``live_accounts`` row deployment/breaker FKs need."""
    import shared.database as database

    database.init_db()
    with database.get_session() as session:
        if session.get(database.LiveAccountModel, account_id) is None:
            session.add(
                database.LiveAccountModel(
                    id=account_id, broker="test", name=account_id
                )
            )
        session.commit()


def _persist_deployment(
    account_id: str, deployment_id: str, *, status: str = "PAUSED"
) -> None:
    """Persist the ``live_deployments`` row ``get_live_deployment`` resolves."""
    import shared.database as database

    _ensure_live_account(account_id)
    database.init_db()
    with database.get_session() as session:
        if session.get(database.LiveDeploymentModel, deployment_id) is None:
            session.add(
                database.LiveDeploymentModel(
                    id=deployment_id,
                    strategy_name="t",
                    account_id=account_id,
                    symbols_json=["AAPL"],
                    timeframe="1h",
                    status=status,
                )
            )
        session.commit()


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


def test_poll_adapter_balance_latches_account_and_deployment_scopes(monkeypatch):
    """Drives the real ``_poll_adapter_balance``: a 6% equity drop latches
    BOTH the account scope and the deployment scope on the poll itself, and
    the deployment row seeds its baseline from its OWN first observation
    (created mid-stream, never inheriting the account baseline). The
    deployment scope only moves with its OWN P&L, so the drop is attributed
    to it via the ledger-P&L lookup."""
    dep = _deployment(account_id="acc-pollwire")
    adapter = _PollAdapter([100_000.0, 94_000.0])

    pnls_script = [{str(dep.id): 0.0}, {str(dep.id): -6_000.0}]

    async def _pnls(_adapter, _account_id):
        return pnls_script.pop(0) if len(pnls_script) > 1 else pnls_script[0]

    monkeypatch.setattr(daemon, "account_deployment_trading_pnls", _pnls)
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


def test_prepare_order_refuses_when_equity_read_is_unparseable():
    """A broker equity read that hits an unreadable money field (cTrader's
    ``get_equity`` raises ``MoneyParseError`` on garbage balance/equity/
    netUnrealizedPnL) must refuse the entry with
    ``LossBreakerBreachError(state_unreadable=True)`` - never submit."""
    from shared.money import MoneyParseError

    account_id = "acc-eqgarbage"
    dep = _deployment(account_id=account_id)
    _observe(account_id, 100_000.0, DAY1)

    class _GarbageEquityAdapter(_Adapter):
        async def get_equity(self):
            raise MoneyParseError("unreadable money value: 'junk'")

    adapter = _GarbageEquityAdapter(equity=100_000.0)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        asyncio.run(
            daemon.prepare_order_with_risk(adapter, dep, _entry_signal(dep))
        )
    assert excinfo.value.state_unreadable is True


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
    # Runs forever by design; stub it like the other gather members so
    # run_daemon can return.
    monkeypatch.setattr(daemon, "run_internal_strategy_loop", _noop_async)
    monkeypatch.setattr(daemon, "run_flip_workflow_loop", _noop_async)
    monkeypatch.setattr(daemon, "run_protective_stop_maintenance_loop", _noop_async)

    monkeypatch.setattr(daemon, "run_broker_reconciliation_loop", _noop_async)
    monkeypatch.setattr(
        daemon,
        "_start_balance_poller",
        lambda *a, **k: asyncio.ensure_future(_noop_async()),
    )
    env_file = tmp_path / "custom.accounts.env"
    asyncio.run(daemon.run_daemon(accounts_env=env_file))
    assert captured["path"] == env_file


# ─── Anchored deployment scope: unattributed equity is not the deployment's ────
#
# Sentry TRADING-PLATFORM-2F: an unattributed broker-side close moved account
# equity and, through ``equity - total_pnl + own_pnl``, moved EVERY deployment
# scope - all seven ACTIVE deployments on the account tripped at once. A
# deployment scope now observes ``equity_anchor + own P&L``: the anchor is the
# residual ``account_equity - total deployment P&L`` fixed at first
# observation, so only the deployment's own trading P&L moves its scope.


def test_unattributed_equity_drop_does_not_trip_deployment_scopes():
    """Poll path: a 33% unattributed drop trips the ACCOUNT scope only."""
    dep_a = _deployment(account_id="acc-foreign")
    dep_b = _deployment(account_id="acc-foreign")
    a_id, b_id = str(dep_a.id), str(dep_b.id)
    deployments = [dep_a, dep_b]
    pnls_flat = {a_id: 0.0, b_id: 0.0}
    # Baseline: account equity 134, no deployment P&L -> residual 134 anchors.
    observe_account_loss_breaker_equity(
        "acc-foreign", 134.0, now=DAY1,
        deployments=deployments, deployment_pnls=pnls_flat, env_values=NO_ENV,
    )
    # The unattributed close: equity 134 -> 90 (-33%), P&L map unchanged.
    observe_account_loss_breaker_equity(
        "acc-foreign", 90.0, now=DAY1 + timedelta(minutes=1),
        deployments=deployments, deployment_pnls=pnls_flat, env_values=NO_ENV,
    )
    account_state = get_live_loss_breaker_state(
        account_loss_breaker_scope("acc-foreign")
    )
    assert account_state.daily_breached is True
    assert account_state.drawdown_breached is True
    for dep in (dep_a, dep_b):
        state = get_live_loss_breaker_state(
            deployment_loss_breaker_scope(str(dep.id))
        )
        assert state.daily_breached is False
        assert state.drawdown_breached is False
        # The scope equity never moved: anchor 134 + own P&L 0.
        assert state.last_equity == pytest.approx(134.0)
        assert state.equity_anchor == pytest.approx(134.0)


def test_unattributed_equity_drop_does_not_trip_deployment_scopes_at_gate():
    """Entry-gate path: same drop, thresholds raised so the account scope
    stays under - the deployment scope must still pass."""
    dep = _deployment(account_id="acc-foreign-gate")
    dep_id = str(dep.id)
    pnls_flat = {dep_id: 0.0}
    # Raised thresholds on BOTH poll and gate so the account scope observes
    # the -33% drop without latching: only the deployment scope's verdict is
    # under test here (the poll-path test covers the account latch).
    env = {
        "LOSS_BREAKER_DAILY_LOSS_FRACTION": "0.5",
        "LOSS_BREAKER_MAX_DRAWDOWN_FRACTION": "0.5",
    }
    observe_account_loss_breaker_equity(
        "acc-foreign-gate", 134.0, now=DAY1,
        deployments=[dep], deployment_pnls=pnls_flat, env_values=env,
    )
    observe_account_loss_breaker_equity(
        "acc-foreign-gate", 90.0, now=DAY1 + timedelta(minutes=1),
        deployments=[dep], deployment_pnls=pnls_flat, env_values=env,
    )
    # Account -33% is under the raised 50% limits; the deployment scope's
    # anchored equity is flat at 134 -> the entry is allowed.
    _gate(
        dep, equity=90.0, now=DAY1 + timedelta(minutes=2),
        env_values=env, deployment_pnls=pnls_flat,
    )
    state = get_live_loss_breaker_state(deployment_loss_breaker_scope(dep_id))
    assert state.last_equity == pytest.approx(134.0)
    assert state.daily_breached is False
    assert state.drawdown_breached is False


def test_own_pnl_loss_still_trips_only_that_deployment_scope():
    """The fix must not blunt the breaker: one deployment's own P&L dropping
    past the threshold trips ITS scope on poll and gate, not the sibling's.
    dep_b's gain offsets dep_a's loss so the ACCOUNT scope stays under."""
    dep_a = _deployment(account_id="acc-ownloss")
    dep_b = _deployment(account_id="acc-ownloss")
    a_id, b_id = str(dep_a.id), str(dep_b.id)
    deployments = [dep_a, dep_b]
    observe_account_loss_breaker_equity(
        "acc-ownloss", 100_000.0, now=DAY1,
        deployments=deployments,
        deployment_pnls={a_id: 0.0, b_id: 0.0},
        env_values=NO_ENV,
    )
    # dep_a -6k, dep_b +4k: account equity -2k (residual stays 100k), so only
    # dep_a's anchored equity moves: 100k -> 94k (-6% > 5% daily).
    pnls = {a_id: -6_000.0, b_id: 4_000.0}
    loss_time = DAY1 + timedelta(minutes=1)
    observe_account_loss_breaker_equity(
        "acc-ownloss", 98_000.0, now=loss_time,
        deployments=deployments, deployment_pnls=pnls, env_values=NO_ENV,
    )
    account_state = get_live_loss_breaker_state(
        account_loss_breaker_scope("acc-ownloss")
    )
    assert account_state.daily_breached is False
    state_a = get_live_loss_breaker_state(deployment_loss_breaker_scope(a_id))
    assert state_a.daily_breached is True
    assert state_a.last_equity == pytest.approx(94_000.0)
    state_b = get_live_loss_breaker_state(deployment_loss_breaker_scope(b_id))
    assert state_b.daily_breached is False
    assert state_b.last_equity == pytest.approx(104_000.0)
    # Gate agrees: dep_a refused on its own scope, dep_b allowed.
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            dep_a, equity=98_000.0, now=loss_time,
            deployment_pnls=pnls,
        )
    assert excinfo.value.scope == deployment_loss_breaker_scope(a_id)
    _gate(
        dep_b, equity=98_000.0, now=loss_time,
        deployment_pnls=pnls,
    )


def test_deposit_does_not_move_deployment_scopes():
    """A deposit raises account equity with zero P&L: the deployment scope's
    anchored equity must stay exactly flat."""
    dep = _deployment(account_id="acc-deposit")
    dep_id = str(dep.id)
    observe_account_loss_breaker_equity(
        "acc-deposit", 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    # +20k deposit: account equity 120k, deployment P&L still flat.
    observe_account_loss_breaker_equity(
        "acc-deposit", 120_000.0, now=DAY1 + timedelta(minutes=1),
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(deployment_loss_breaker_scope(dep_id))
    assert state.last_equity == pytest.approx(100_000.0)
    assert state.equity_anchor == pytest.approx(100_000.0)
    assert state.daily_breached is False
    assert state.drawdown_breached is False
    # The account scope saw the deposit, as it must.
    account_state = get_live_loss_breaker_state(
        account_loss_breaker_scope("acc-deposit")
    )
    assert account_state.last_equity == pytest.approx(120_000.0)


def test_reset_drawdown_reanchors_deployment_scope():
    """``reset-drawdown`` keeps the anchored series consistent: the anchor
    shifts by the re-base delta so scope equity continues from the reset
    equity and keeps measuring only own P&L."""
    from live_runner.loss_breaker import reset_drawdown_breaker

    dep = _deployment(account_id="acc-reset")
    dep_id = str(dep.id)
    scope = deployment_loss_breaker_scope(dep_id)
    observe_account_loss_breaker_equity(
        "acc-reset", 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    # Unattributed -30k drop: the deployment scope stays at 100k.
    observe_account_loss_breaker_equity(
        "acc-reset", 70_000.0, now=DAY1 + timedelta(minutes=1),
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(100_000.0)
    assert state.equity_anchor == pytest.approx(100_000.0)
    # Operator reset at the scope's own last equity: the delta is zero, the
    # anchor is unchanged and the peak re-bases.
    updated = reset_drawdown_breaker(scope, equity=100_000.0, now=DAY1)
    assert updated.peak_equity == pytest.approx(100_000.0)
    assert updated.equity_anchor == pytest.approx(100_000.0)
    # A reset to a DIFFERENT equity shifts the anchor by the same delta.
    updated = reset_drawdown_breaker(scope, equity=120_000.0, now=DAY1)
    assert updated.equity_anchor == pytest.approx(120_000.0)
    # Next observation: equity = anchor + own P&L = 120k - 10k = 110k.
    observe_account_loss_breaker_equity(
        "acc-reset", 70_000.0, now=DAY1 + timedelta(minutes=2),
        deployments=[dep], deployment_pnls={dep_id: -10_000.0},
        env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(110_000.0)


def test_legacy_deployment_scope_anchors_on_next_observation_without_tripping():
    """A deployment row written before ``equity_anchor`` existed (NULL)
    anchors on its next observation CONTINUING from its last observed
    equity - the anchoring observation itself cannot trip the breaker, and
    unattributed drift afterwards still cannot move the scope."""
    dep = _deployment(account_id="acc-legacy")
    dep_id = str(dep.id)
    scope = deployment_loss_breaker_scope(dep_id)
    # Simulate the pre-anchor row: written through the raw DB helper, which
    # never sets equity_anchor (NULL after the column migration).
    observe_live_loss_breaker(scope, "acc-legacy", 100_000.0, DAY1)
    observe_live_loss_breaker(
        scope, "acc-legacy", 98_000.0, DAY1 + timedelta(minutes=1)
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor is None
    # A -31% unattributed account drop with flat deployment P&L: the legacy
    # row's first anchored observation continues from 98k (last_equity), it
    # does NOT re-measure the whole residual drop.
    observe_account_loss_breaker_equity(
        "acc-legacy", 69_000.0, now=DAY1 + timedelta(minutes=2),
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(98_000.0)
    assert state.last_equity == pytest.approx(98_000.0)
    assert state.daily_breached is False
    assert state.drawdown_breached is False
    # And a further unattributed drop still cannot move the now-anchored scope.
    observe_account_loss_breaker_equity(
        "acc-legacy", 50_000.0, now=DAY1 + timedelta(minutes=3),
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(98_000.0)
    assert state.daily_breached is False
    assert state.drawdown_breached is False


# ─── Anchor arithmetic with NON-ZERO own P&L ──────────────────────────────────
#
# The migration path (legacy NULL-anchor row) and the first-observation path
# (no row) are where every live deployment lands; the arithmetic there must
# be pinned with a non-zero own P&L or mutants that drop ``- own_pnl`` from
# the persisted anchor survive silently.


def _legacy_unanchored_row(account_id: str, dep: LiveDeploymentConfig, last_equity: float):
    """A pre-anchor deployment row: ``equity_anchor`` stays NULL."""
    scope = deployment_loss_breaker_scope(str(dep.id))
    observe_live_loss_breaker(scope, account_id, last_equity, DAY1)
    observe_live_loss_breaker(
        scope, account_id, last_equity, DAY1 + timedelta(minutes=1)
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor is None
    assert state.last_equity == pytest.approx(last_equity)
    return scope


def test_legacy_row_anchor_subtracts_own_pnl_poll():
    """Poll path: a legacy row with ``last_equity = L`` and own P&L 300 keeps
    ``last_equity == L`` and persists ``equity_anchor == L - 300``; the next
    observation with own P&L 250 lands at ``L - 50`` - it moves by exactly
    the change in own P&L."""
    account_id = "acc-anchor-legacy-poll"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    last = 100_000.0
    scope = _legacy_unanchored_row(account_id, dep, last)

    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1 + timedelta(minutes=2),
        deployments=[dep], deployment_pnls={dep_id: 300.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(last)
    assert state.equity_anchor == pytest.approx(last - 300.0)

    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1 + timedelta(minutes=3),
        deployments=[dep], deployment_pnls={dep_id: 250.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(last - 300.0)
    assert state.last_equity == pytest.approx(last - 50.0)


def test_legacy_row_anchor_subtracts_own_pnl_gate():
    """Entry-gate path: the same legacy-row arithmetic through
    ``refuse_entry_if_loss_breaker_tripped``."""
    account_id = "acc-anchor-legacy-gate"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    last = 100_000.0
    scope = _legacy_unanchored_row(account_id, dep, last)

    _gate(
        dep, equity=100_000.0, now=DAY1 + timedelta(minutes=2),
        deployment_pnls={dep_id: 300.0},
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(last)
    assert state.equity_anchor == pytest.approx(last - 300.0)

    _gate(
        dep, equity=100_000.0, now=DAY1 + timedelta(minutes=3),
        deployment_pnls={dep_id: 250.0},
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(last - 300.0)
    assert state.last_equity == pytest.approx(last - 50.0)


def test_first_observation_anchor_is_residual_poll():
    """Poll path: a deployment whose FIRST breaker observation already has
    fills anchors at the residual ``account_equity - total P&L``; the second
    observation moves by exactly the change in own P&L."""
    account_id = "acc-anchor-first-poll"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    scope = deployment_loss_breaker_scope(dep_id)
    account_equity = 100_000.0

    observe_account_loss_breaker_equity(
        account_id, account_equity, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 300.0}, env_values=NO_ENV,
    )
    residual = account_equity - 300.0
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(residual)
    assert state.last_equity == pytest.approx(residual + 300.0)

    observe_account_loss_breaker_equity(
        account_id, account_equity, now=DAY1 + timedelta(minutes=1),
        deployments=[dep], deployment_pnls={dep_id: 250.0}, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    # own P&L fell by 50: the scope equity falls by exactly 50.
    assert state.last_equity == pytest.approx(account_equity - 50.0)
    assert state.equity_anchor == pytest.approx(residual)


def test_first_observation_anchor_is_residual_gate():
    """Entry-gate path: same first-observation residual arithmetic."""
    account_id = "acc-anchor-first-gate"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    scope = deployment_loss_breaker_scope(dep_id)
    account_equity = 100_000.0

    _gate(
        dep, equity=account_equity, now=DAY1,
        deployment_pnls={dep_id: 300.0},
    )
    residual = account_equity - 300.0
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(residual)
    assert state.last_equity == pytest.approx(residual + 300.0)

    _gate(
        dep, equity=account_equity, now=DAY1 + timedelta(minutes=1),
        deployment_pnls={dep_id: 250.0},
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(account_equity - 50.0)
    assert state.equity_anchor == pytest.approx(residual)


def test_equity_anchor_is_write_once_at_db_level():
    """``observe_live_loss_breaker`` fills ``equity_anchor`` only while the
    row's anchor is NULL; a later observation must never rewrite it."""
    scope = deployment_loss_breaker_scope("dep-writeonce")
    observe_live_loss_breaker(
        scope, "acc-writeonce", 100.0, DAY1, equity_anchor=50.0
    )
    observe_live_loss_breaker(
        scope, "acc-writeonce", 90.0,
        DAY1 + timedelta(minutes=1), equity_anchor=10.0,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(50.0)


def test_observe_rejects_non_finite_equity_anchor():
    """A NaN/inf anchor must never reach the row: ``observe`` refuses it."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="anchor"):
            observe_live_loss_breaker(
                deployment_loss_breaker_scope("dep-nan-anchor"),
                "acc-nan-anchor",
                100.0,
                DAY1,
                equity_anchor=bad,
            )


def test__deployment_scope_equity_rejects_non_finite_inputs():
    """Every input to the anchored-equity formula must be finite: a NaN
    account equity or a NaN sibling P&L is refused, never propagated into a
    scope equity a caller could act on."""
    from live_runner.loss_breaker import _deployment_scope_equity

    with pytest.raises(ValueError, match="account equity"):
        _deployment_scope_equity(
            scope="deployment:x", account_equity=float("nan"),
            own_pnl=0.0, deployment_pnls={"x": 0.0},
        )
    with pytest.raises(ValueError, match="own P&L"):
        _deployment_scope_equity(
            scope="deployment:x", account_equity=100.0,
            own_pnl=float("nan"), deployment_pnls={"x": float("nan")},
        )
    with pytest.raises(ValueError, match="dep-b"):
        _deployment_scope_equity(
            scope="deployment:x", account_equity=100.0,
            own_pnl=0.0, deployment_pnls={"x": 0.0, "dep-b": float("inf")},
        )


def test_observe_rejects_anchor_on_account_scope():
    """Account scopes never carry an equity anchor; ``observe`` refuses one
    rather than storing an invariant violation."""
    with pytest.raises(ValueError, match="account scopes never carry"):
        observe_live_loss_breaker(
            account_loss_breaker_scope("acc-anchor-forbidden"),
            "acc-anchor-forbidden",
            100.0,
            DAY1,
            equity_anchor=50.0,
        )


def test_gate_fails_closed_when_deployment_state_read_raises(monkeypatch):
    """The gate's fail-closed wrapper: a breaker-state read that raises for
    the deployment scope must surface as ``state_unreadable``, never as a
    raw exception and never as 'all clear'. The deployment scope's equity is
    computed inside the observe write transaction, so the seam that can
    raise is ``observe_live_loss_breaker_series``."""
    account_id = "acc-state-unreadable"
    dep = _deployment(account_id=account_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("breaker state is gone")

    monkeypatch.setattr(loss_breaker, "observe_live_loss_breaker_series", _boom)
    with pytest.raises(LossBreakerBreachError) as excinfo:
        _gate(
            dep, equity=100_000.0, now=DAY1,
            deployment_pnls={str(dep.id): 0.0},
        )
    assert excinfo.value.state_unreadable is True
    assert excinfo.value.scope == deployment_loss_breaker_scope(str(dep.id))


# ─── Operator re-anchor ───────────────────────────────────────────────────────


def test_reanchor_shifts_series_and_keeps_latch():
    """``re-anchor`` sets the anchor to the CURRENT residual, moves the whole
    equity series by the same delta, keeps BOTH latches, leaves
    ``last_observed_at`` alone and writes an audit row."""
    from shared.database import latch_live_loss_breaker_breach, list_live_control_audit

    account_id = "acc-reanchor"
    dep_a = _deployment(account_id=account_id)
    dep_b = _deployment(account_id=account_id)
    a_id, b_id = str(dep_a.id), str(dep_b.id)
    _persist_deployment(account_id, a_id)
    _persist_deployment(account_id, b_id)
    deployments = [dep_a, dep_b]
    # equity 100k, pnls a=-6k b=+4k -> total -2k, residual 102k.
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=deployments,
        deployment_pnls={a_id: -6_000.0, b_id: 4_000.0},
        env_values=NO_ENV,
    )
    scope_a = deployment_loss_breaker_scope(a_id)
    before = get_live_loss_breaker_state(scope_a)
    assert before.equity_anchor == pytest.approx(102_000.0)
    assert before.last_equity == pytest.approx(96_000.0)
    latch_live_loss_breaker_breach(
        scope_a, "drawdown", {"kind": "drawdown"}, DAY1
    )
    latch_live_loss_breaker_breach(
        scope_a, "daily", {"kind": "daily"}, DAY1
    )

    # 80k withdrawn; deployment P&L unchanged (implied by the rows).
    updated = reanchor_deployment_loss_breaker_scope(
        scope_a, account_equity=20_000.0, actor="test",
        now=DAY1 + timedelta(minutes=5),
    )
    # residual = 20_000 - (-2_000) = 22_000; own_a = -6_000 -> 16_000.
    assert updated.equity_anchor == pytest.approx(22_000.0)
    assert updated.last_equity == pytest.approx(16_000.0)
    # delta = 16_000 - 96_000 = -80_000 moved the whole series.
    assert updated.day_start_equity == pytest.approx(16_000.0)
    assert updated.peak_equity == pytest.approx(16_000.0)
    assert updated.drawdown_breached is True
    assert updated.daily_breached is True
    # A re-anchor is not a market observation: the observation timestamp
    # the freshness rules measure against must not move.
    assert updated.last_observed_at == before.last_observed_at

    audits = [
        a for a in list_live_control_audit(target_id=scope_a)
        if a["action"] == "loss_breaker.reanchor"
    ]
    assert len(audits) == 1
    assert audits[0]["actor"] == "test"
    assert audits[0]["succeeded"] is True
    assert audits[0]["detail"]["equity_anchor_after"] == pytest.approx(22_000.0)

    # The sibling scope is untouched.
    state_b = get_live_loss_breaker_state(deployment_loss_breaker_scope(b_id))
    assert state_b.equity_anchor == pytest.approx(102_000.0)
    assert state_b.last_equity == pytest.approx(106_000.0)


def test_reanchor_refuses_account_scope_and_bad_inputs():
    account_id = "acc-reanchor-refuse"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    scope = deployment_loss_breaker_scope(dep_id)

    with pytest.raises(ValueError, match="deployment scopes only"):
        reanchor_deployment_loss_breaker_scope(
            account_loss_breaker_scope(account_id), account_equity=50_000.0
        )
    with pytest.raises(ValueError, match="no loss breaker state"):
        reanchor_deployment_loss_breaker_scope(
            deployment_loss_breaker_scope("dep-missing"), account_equity=50_000.0
        )
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="positive finite"):
            reanchor_deployment_loss_breaker_scope(
                scope, account_equity=bad
            )
    # Nothing was written by the refused attempts.
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(100_000.0)


def test_reanchor_refuses_when_deployment_pnl_is_unmeasurable():
    """A deployment with fills but no ANCHORED scope row makes the residual
    unknowable: the re-anchor must refuse rather than guess."""
    account_id = "acc-reanchor-unmeasurable"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    scope = deployment_loss_breaker_scope(dep_id)

    # A second deployment on the account has fills but was never observed:
    # no scope row at all.
    other = _deployment(account_id=account_id)
    _persist_fill(account_id, str(other.id), "AAPL", "buy", 10.0, 100.0)
    with pytest.raises(ValueError, match="no anchored breaker scope"):
        reanchor_deployment_loss_breaker_scope(
            scope, account_equity=50_000.0
        )

    # And a scope row that exists but has not anchored yet, WITH fills,
    # refuses the same way on a fresh account.
    account_id2 = "acc-reanchor-unanchored"
    dep2 = _deployment(account_id=account_id2)
    dep2_id = str(dep2.id)
    scope2 = deployment_loss_breaker_scope(dep2_id)
    observe_live_loss_breaker(scope2, account_id2, 90_000.0, DAY1)
    _persist_fill(account_id2, dep2_id, "AAPL", "buy", 10.0, 100.0)
    with pytest.raises(ValueError, match="has not anchored yet"):
        reanchor_deployment_loss_breaker_scope(
            scope2, account_equity=50_000.0
        )

    # An unanchored row WITHOUT fills contributes exactly 0 and is fine.
    account_id3 = "acc-reanchor-clean"
    dep3 = _deployment(account_id=account_id3)
    dep3_id = str(dep3.id)
    _persist_deployment(account_id3, dep3_id)
    scope3 = deployment_loss_breaker_scope(dep3_id)
    observe_live_loss_breaker(scope3, account_id3, 90_000.0, DAY1)
    updated = reanchor_deployment_loss_breaker_scope(
        scope3, account_equity=50_000.0, now=DAY1 + timedelta(minutes=1)
    )
    assert updated.equity_anchor == pytest.approx(50_000.0)
    assert updated.last_equity == pytest.approx(50_000.0)


def test_reanchor_after_account_repoint_uses_new_account_residual():
    """B1: after ``update_live_deployment_account`` the re-anchor must
    measure the deployment's CURRENT account - its equity source, its
    scope list and its P&L total - not the account the breaker row was
    created on. The anchor must equal the NEW account's residual."""
    old_account = "acc-repoint-old"
    new_account = "acc-repoint-new"
    dep_x = _deployment(account_id=old_account)
    dep_y = _deployment(account_id=old_account)
    x_id, y_id = str(dep_x.id), str(dep_y.id)
    _persist_deployment(old_account, x_id)
    _persist_deployment(old_account, y_id)
    _ensure_live_account(new_account)
    # equity 100k, pnls x=-6k y=+2k -> total -4k, residual 104k.
    observe_account_loss_breaker_equity(
        old_account, 100_000.0, now=DAY1,
        deployments=[dep_x, dep_y],
        deployment_pnls={x_id: -6_000.0, y_id: 2_000.0},
        env_values=NO_ENV,
    )
    scope_x = deployment_loss_breaker_scope(x_id)
    assert get_live_loss_breaker_state(scope_x).account_id == old_account

    # Re-point X at the new account and observe it there at 10k equity.
    # The poll loads deployments fresh, so it sees X on the new account.
    update_live_deployment_account(x_id, new_account)
    dep_x_moved = _deployment(account_id=new_account, deployment_id=x_id)
    observe_account_loss_breaker_equity(
        new_account, 10_000.0, now=DAY1 + timedelta(minutes=1),
        deployments=[dep_x_moved],
        deployment_pnls={x_id: -6_000.0},
        env_values=NO_ENV,
    )
    # The observe moved the row to the new account: the old account's scope
    # list (and its re-anchor total) no longer counts the moved deployment.
    moved = get_live_loss_breaker_state(scope_x)
    assert moved.account_id == new_account
    old_scopes = {row.scope for row in list_live_loss_breaker_states(old_account)}
    assert scope_x not in old_scopes
    new_scopes = {row.scope for row in list_live_loss_breaker_states(new_account)}
    assert scope_x in new_scopes

    updated = reanchor_deployment_loss_breaker_scope(
        scope_x, account_equity=10_000.0, actor="test",
        now=DAY1 + timedelta(minutes=2),
    )
    # NEW account residual = 10_000 - (-6_000) = 16_000. On the OLD account
    # the total would wrongly include Y's +2_000 (residual 14_000).
    assert updated.equity_anchor == pytest.approx(16_000.0)
    assert updated.account_id == new_account
    assert updated.last_equity == pytest.approx(10_000.0)
    # The sibling scope on the old account is untouched.
    state_y = get_live_loss_breaker_state(deployment_loss_breaker_scope(y_id))
    assert state_y.account_id == old_account
    assert state_y.equity_anchor == pytest.approx(104_000.0)


def test_reanchor_after_repoint_without_observe_uses_new_account():
    """B1 pin with the daemon stopped: no observe moves the breaker row after
    the re-point, so the re-anchor's own account resolution is all there is.
    The anchor must be the NEW account's residual and the row must follow the
    deployment to the new account."""
    old_account = "acc-repoint-noobserve-old"
    new_account = "acc-repoint-noobserve-new"
    dep = _deployment(account_id=old_account)
    dep_id = str(dep.id)
    _persist_deployment(old_account, dep_id)
    _ensure_live_account(new_account)
    # Old account: own P&L -10k -> anchor 110k, scope equity 100k. On the old
    # account a re-anchor would wrongly keep that -10k in the residual.
    observe_account_loss_breaker_equity(
        old_account,
        100_000.0,
        now=DAY1,
        deployments=[dep],
        deployment_pnls={dep_id: -10_000.0},
        env_values=NO_ENV,
    )
    scope = deployment_loss_breaker_scope(dep_id)
    assert get_live_loss_breaker_state(scope).account_id == old_account

    update_live_deployment_account(dep_id, new_account)
    # Account-scope observation only: the deployment row is NOT observed on
    # the new account, so its stored account_id is still the old one.
    observe_account_loss_breaker_equity(
        new_account,
        70_000.0,
        now=DAY1 + timedelta(minutes=1),
        deployments=[],
        deployment_pnls={},
        env_values=NO_ENV,
    )
    assert get_live_loss_breaker_state(scope).account_id == old_account

    updated = reanchor_deployment_loss_breaker_scope(
        scope,
        account_equity=70_000.0,
        actor="test",
        now=DAY1 + timedelta(minutes=2),
    )
    # The new account has no other deployment P&L: residual == equity.
    assert updated.equity_anchor == pytest.approx(70_000.0)
    assert updated.account_id == new_account
    assert updated.last_equity == pytest.approx(70_000.0)
    old_scopes = {row.scope for row in list_live_loss_breaker_states(old_account)}
    new_scopes = {row.scope for row in list_live_loss_breaker_states(new_account)}
    assert scope not in old_scopes
    assert scope in new_scopes


def test_reanchor_after_repoint_with_fills_refuses_until_observed():
    """A re-pointed deployment WITH fills cannot be re-anchored before its
    breaker row has been observed on the new account: the fill ledger follows
    the deployment's current account, but the stale row still sits on the old
    account's scope list, so the residual is unknowable."""
    old_account = "acc-repoint-fills-old"
    new_account = "acc-repoint-fills-new"
    dep = _deployment(account_id=old_account)
    dep_id = str(dep.id)
    _persist_deployment(old_account, dep_id)
    _ensure_live_account(new_account)
    observe_account_loss_breaker_equity(
        old_account,
        100_000.0,
        now=DAY1,
        deployments=[dep],
        deployment_pnls={dep_id: 0.0},
        env_values=NO_ENV,
    )
    scope = deployment_loss_breaker_scope(dep_id)
    _persist_fill(old_account, dep_id, "AAPL", "buy", 10.0, 100.0)

    update_live_deployment_account(dep_id, new_account)
    reanchor_now = DAY1 + timedelta(minutes=1)
    with pytest.raises(ValueError, match="no anchored breaker scope"):
        reanchor_deployment_loss_breaker_scope(
            scope,
            account_equity=50_000.0,
            now=reanchor_now,
        )
    state = get_live_loss_breaker_state(scope)
    assert state.account_id == old_account
    assert state.equity_anchor == pytest.approx(100_000.0)


def test_reanchor_refuses_missing_deployment_row():
    """The scope's account is resolved from the deployment row; a breaker
    scope with no live deployment behind it cannot be re-anchored."""
    account_id = "acc-reanchor-nodep"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    # Breaker state exists but no live_deployments row was persisted.
    scope = deployment_loss_breaker_scope(dep_id)
    reanchor_now = DAY1 + timedelta(minutes=1)
    with pytest.raises(ValueError, match="no live deployment"):
        reanchor_deployment_loss_breaker_scope(
            scope, account_equity=50_000.0, now=reanchor_now
        )


def test_reanchor_refuses_unresolved_ledger_flag():
    """An unresolved ``ledger_reconciliation_required`` flag makes the
    fill-ledger P&L untrustworthy - the same condition the live path fails
    closed on - so the re-anchor refuses."""
    from shared.database import record_live_control_audit

    account_id = "acc-reanchor-flag"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    record_live_control_audit(
        action="ledger_reconciliation_required",
        target_type="account_symbol",
        target_id=f"{account_id}:AAPL",
        actor="test",
        detail={"cause": "unattributed close"},
    )
    scope = deployment_loss_breaker_scope(dep_id)
    reanchor_now = DAY1 + timedelta(minutes=1)
    with pytest.raises(ValueError, match="ledger_reconciliation_required"):
        reanchor_deployment_loss_breaker_scope(
            scope,
            account_equity=50_000.0,
            now=reanchor_now,
        )


def test_reanchor_refuses_untrusted_fill():
    """A fill whose price cannot be trusted makes the deployment P&L
    unmeasurable; the re-anchor refuses rather than anchor on it."""
    account_id = "acc-reanchor-untrusted"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    _persist_fill(account_id, dep_id, "AAPL", "buy", 10.0, 100.0, observed="absent")
    scope = deployment_loss_breaker_scope(dep_id)
    reanchor_now = DAY1 + timedelta(minutes=1)
    with pytest.raises(ValueError, match="trusted price"):
        reanchor_deployment_loss_breaker_scope(
            scope,
            account_equity=50_000.0,
            now=reanchor_now,
        )


def test_reanchor_refuses_stale_deployment_row():
    """A deployment row with fills whose last observation is older than
    ``STALE_SNAPSHOT_SECONDS`` before the equity time carries a stale P&L;
    the re-anchor refuses rather than skew the residual."""
    account_id = "acc-reanchor-stale"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    stale_day = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=stale_day,
        deployments=[dep], deployment_pnls={dep_id: 0.0}, env_values=NO_ENV,
    )
    _persist_fill(account_id, dep_id, "AAPL", "buy", 10.0, 100.0)
    scope = deployment_loss_breaker_scope(dep_id)
    reanchor_now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="last observed"):
        reanchor_deployment_loss_breaker_scope(
            scope,
            account_equity=50_000.0,
            now=reanchor_now,
        )


def test_reanchor_refuses_deployment_row_after_equity_observation():
    """A fills-bearing deployment row observed AFTER the equity snapshot is
    just as unusable as a stale one: its P&L postdates the equity it would be
    subtracted from."""
    account_id = "acc-reanchor-future-row"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    row_time = DAY1 + timedelta(minutes=10)
    observe_account_loss_breaker_equity(
        account_id,
        100_000.0,
        now=row_time,
        deployments=[dep],
        deployment_pnls={dep_id: 0.0},
        env_values=NO_ENV,
    )
    _persist_fill(account_id, dep_id, "AAPL", "buy", 10.0, 100.0)
    scope = deployment_loss_breaker_scope(dep_id)
    with pytest.raises(ValueError, match="AFTER the equity observation"):
        reanchor_deployment_loss_breaker_scope(
            scope,
            account_equity=50_000.0,
            now=row_time,
            equity_observed_at=DAY1,
        )


def test_cross_connection_reanchor_cannot_interleave_with_observe(monkeypatch):
    """S1 pin: the process-local observe lock is not the serialisation.

    The CLI runs in another process, so this bypasses
    ``_loss_breaker_observe_lock`` and lets the re-anchor thread race the
    observe's ``BEGIN IMMEDIATE`` window for real. A stale series write must
    not land on the re-anchored row and false-latch the scope.
    """
    import contextlib
    import threading

    import shared.database as database

    monkeypatch.setattr(
        database,
        "_loss_breaker_observe_lock",
        lambda _scope: contextlib.nullcontext(),
    )
    account_id = "acc-xconn"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    scope = deployment_loss_breaker_scope(dep_id)
    pnls = {dep_id: -1_000.0}
    observe_account_loss_breaker_equity(
        account_id,
        100_000.0,
        now=DAY1,
        deployments=[dep],
        deployment_pnls=pnls,
        env_values=NO_ENV,
    )

    real_series = loss_breaker._deployment_scope_series
    reanchor_thread: threading.Thread | None = None
    reanchor_errors: list[BaseException] = []

    def _reanchor() -> None:
        try:
            reanchor_deployment_loss_breaker_scope(
                scope,
                account_equity=200_000.0,
                actor="test",
                now=DAY1 + timedelta(minutes=2),
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            reanchor_errors.append(exc)

    def hooked(record, **kwargs):
        nonlocal reanchor_thread
        out = real_series(record, **kwargs)
        if reanchor_thread is None:
            reanchor_thread = threading.Thread(target=_reanchor, daemon=True)
            reanchor_thread.start()
            # With BEGIN IMMEDIATE this join times out: the re-anchor waits
            # for the observe's commit. Without it the re-anchor can commit
            # inside the resolver window and the stale write below lands on
            # the shifted series.
            reanchor_thread.join(timeout=2.0)
        return out

    monkeypatch.setattr(loss_breaker, "_deployment_scope_series", hooked)
    pnls2 = {dep_id: -1_500.0}
    observe_account_loss_breaker_equity(
        account_id,
        99_500.0,
        now=DAY1 + timedelta(minutes=2),
        deployments=[dep],
        deployment_pnls=pnls2,
        env_values=NO_ENV,
    )
    assert reanchor_thread is not None
    reanchor_thread.join(timeout=40)
    monkeypatch.setattr(loss_breaker, "_deployment_scope_series", real_series)
    assert reanchor_errors == []

    observe_account_loss_breaker_equity(
        account_id,
        200_000.0,
        now=DAY1 + timedelta(minutes=4),
        deployments=[dep],
        deployment_pnls=pnls2,
        env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(201_000.0)
    assert state.last_equity == pytest.approx(199_500.0)
    assert state.daily_breached is False
    assert state.drawdown_breached is False


def test_observe_and_reanchor_serialize_without_false_latch(monkeypatch):
    """S1: a ``re-anchor`` attempted while a deployment observe holds its
    write transaction must not leave the series on the stale anchor. The
    observe computes equity from the stored row inside ``BEGIN IMMEDIATE``,
    so whichever commits second sees a consistent series - and the next
    observation cannot false-latch."""
    import threading
    import time

    import shared.database as database

    account_id = "acc-interleave"
    dep = _deployment(account_id=account_id)
    dep_id = str(dep.id)
    _persist_deployment(account_id, dep_id)
    scope = deployment_loss_breaker_scope(dep_id)
    pnls = {dep_id: -1_000.0}
    # Seed: anchor 101k, last 100k.
    observe_account_loss_breaker_equity(
        account_id, 100_000.0, now=DAY1,
        deployments=[dep], deployment_pnls=pnls, env_values=NO_ENV,
    )

    inside_txn = threading.Event()
    reanchor_go = threading.Event()
    reanchor_done = threading.Event()
    fired: list[bool] = []
    real_apply = database._apply_loss_breaker_observation

    def hooked_apply(row, **kwargs):
        # Runs inside the observe write transaction, after the row read and
        # before the series write - the exact window the race lived in.
        if row.scope == scope and not fired:
            fired.append(True)
            inside_txn.set()
            assert reanchor_go.wait(timeout=10)
            # Give the concurrent re-anchor room to commit IF it can: with
            # BEGIN IMMEDIATE it cannot (it waits for our commit); without
            # it, it lands here and the stale write below corrupts the series.
            time.sleep(1.0)
        return real_apply(row, **kwargs)

    monkeypatch.setattr(database, "_apply_loss_breaker_observation", hooked_apply)

    def _reanchor() -> None:
        assert inside_txn.wait(timeout=10)
        reanchor_go.set()
        reanchor_deployment_loss_breaker_scope(
            scope, account_equity=50_000.0, actor="test",
            now=DAY1 + timedelta(minutes=3),
        )
        reanchor_done.set()

    thread = threading.Thread(target=_reanchor, daemon=True)
    thread.start()
    try:
        observe_account_loss_breaker_equity(
            account_id, 100_000.0, now=DAY1 + timedelta(minutes=2),
            deployments=[dep], deployment_pnls=pnls, env_values=NO_ENV,
        )
    finally:
        thread.join(timeout=30)
    assert reanchor_done.is_set()
    monkeypatch.undo()

    # The re-anchor ran after the observe committed: residual 51k, own -1k.
    state = get_live_loss_breaker_state(scope)
    assert state.equity_anchor == pytest.approx(51_000.0)
    assert state.last_equity == pytest.approx(50_000.0)
    assert state.day_start_equity == pytest.approx(50_000.0)
    assert state.peak_equity == pytest.approx(50_000.0)

    # The follow-up observation sees anchor + own = 50k - nothing moved, so
    # no breaker fraction can trip: no false latch from a stale write.
    observe_account_loss_breaker_equity(
        account_id, 50_000.0, now=DAY1 + timedelta(minutes=4),
        deployments=[dep], deployment_pnls=pnls, env_values=NO_ENV,
    )
    state = get_live_loss_breaker_state(scope)
    assert state.last_equity == pytest.approx(50_000.0)
    assert state.daily_breached is False
    assert state.drawdown_breached is False


def test_observe_rejects_non_finite_equity_at_db_level():
    """``observe_live_loss_breaker`` itself refuses NaN/inf equity - the
    poll's own validation is not the only layer."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="must be finite"):
            observe_live_loss_breaker(
                account_loss_breaker_scope("acc-nan-equity"),
                "acc-nan-equity",
                bad,
                DAY1,
            )


def test_reset_rejects_non_finite_equity_at_db_level():
    """``reset_live_loss_breaker_drawdown`` refuses NaN/inf equity itself."""
    observe_live_loss_breaker(
        account_loss_breaker_scope("acc-nan-reset"),
        "acc-nan-reset",
        100_000.0,
        DAY1,
    )
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="must be finite"):
            reset_live_loss_breaker_drawdown(
                account_loss_breaker_scope("acc-nan-reset"), bad, DAY1
            )


def test_reanchor_db_rejects_account_scope():
    """The storage-layer re-anchor refuses account scopes on its own - the
    CLI and library checks are not the only layer."""
    with pytest.raises(ValueError, match="deployment scopes only"):
        reanchor_live_loss_breaker_deployment(
            account_loss_breaker_scope("acc-db-scope"),
            equity_anchor=1.0,
            reanchored_at=DAY1,
            account_id="acc-db-scope",
        )
