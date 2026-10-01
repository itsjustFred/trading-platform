import pytest

from live_runner.ctrader_deployment import build_ctrader_deployment_plan, top_backtests_for_ctrader
from shared.models import LiveAccountConfig


def test_top_backtests_for_ctrader_ranks_by_live_score() -> None:
    rows = [
        {
            "id": "slow",
            "strategy_name": "slow",
            "symbols": ["EURUSD"],
            "timeframe": "1h",
            "sharpe_ratio": 0.5,
            "cagr": 0.1,
            "psr": 0.4,
            "max_drawdown": -5,
            "trade_count": 10,
            "oos_sharpe": 0.4,
        },
        {
            "id": "fast",
            "strategy_name": "fast",
            "symbols": ["XAUUSD"],
            "timeframe": "15m",
            "sharpe_ratio": 2.0,
            "cagr": 0.3,
            "psr": 0.9,
            "max_drawdown": -8,
            "trade_count": 20,
            "oos_sharpe": 1.2,
        },
    ]

    ranked = top_backtests_for_ctrader(rows, limit=1)

    assert ranked[0]["id"] == "fast"
    assert ranked[0]["live_deployment_score"] > 0


def test_build_ctrader_deployment_plan_targets_first_ctrader_account() -> None:
    deployments = build_ctrader_deployment_plan(
        backtests=[
            {
                "id": "bt-1",
                "strategy_name": "fx_breakout",
                "symbols": ["EURUSD"],
                "timeframe": "15m",
                "sharpe_ratio": 1.5,
                "cagr": 0.2,
                "psr": 0.8,
            "max_drawdown": -6,
            "trade_count": 12,
            "oos_status": "available",
        }
        ],
        accounts=[LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")],
        limit=5,
        status="PAUSED",
    )

    assert len(deployments) == 1
    assert deployments[0].account_id == "ct-1"
    assert deployments[0].status == "PAUSED"
    assert deployments[0].signal_config["source_backtest_id"] == "bt-1"


def test_top_backtests_require_oos_evidence_by_default() -> None:
    """C5: in-sample-only rows never reach a live shortlist."""
    rows = [
        {
            "id": "no-oos",
            "strategy_name": "no-oos",
            "symbols": ["EURUSD"],
            "timeframe": "1h",
            "sharpe_ratio": 5.0,
            "cagr": 0.9,
            "psr": 0.99,
            "max_drawdown": -1,
            "trade_count": 100,
        },
    ]
    assert top_backtests_for_ctrader(rows, limit=5) == []
    assert len(top_backtests_for_ctrader(rows, limit=5, require_oos_evidence=False)) == 1


def _backtest(**overrides):
    row = {
        "id": "bt-1",
        "strategy_name": "fx_breakout",
        "symbols": ["EURUSD"],
        "timeframe": "15m",
        "sharpe_ratio": 1.5,
        "cagr": 0.2,
        "psr": 0.8,
        "max_drawdown": -6,
        "trade_count": 12,
        "oos_status": "available",
    }
    row.update(overrides)
    return row


def test_only_a_ctrader_account_is_ever_selected() -> None:
    """The broker filter is the boundary that keeps this a *cTrader* bulk deploy.

    Dropping it left every collected test green, because the only existing plan test
    passes a single cTrader account - so `list(accounts)` and the filtered list are
    indistinguishable whenever every account is already a cTrader one.

    A non-cTrader account must never receive a cTrader deployment: the plan carries
    `signal_source_type="internal"` and the account's own broker decides what that
    means, so this would aim a strategy selected by cTrader criteria at a different
    broker entirely.
    """
    deployments = build_ctrader_deployment_plan(
        backtests=[_backtest()],
        accounts=[
            LiveAccountConfig(id="alp-1", broker="alpaca", name="Alpaca"),
            LiveAccountConfig(id="alp-2", broker="alpaca", name="Alpaca 2"),
        ],
        limit=5,
        status="PAUSED",
    )
    assert deployments == [], "an account with no cTrader broker was selected"

    # And the cTrader one is still chosen when it is present after the others.
    mixed = build_ctrader_deployment_plan(
        backtests=[_backtest()],
        accounts=[
            LiveAccountConfig(id="alp-1", broker="alpaca", name="Alpaca"),
            LiveAccountConfig(id="ct-9", broker="ctrader", name="cTrader"),
        ],
        limit=5,
        status="PAUSED",
    )
    assert [d.account_id for d in mixed] == ["ct-9"]


def test_an_unrecognised_status_plans_as_paused_not_as_itself() -> None:
    """Garbage in, PAUSED out. The allow-list is the fail-closed half of the default.

    The endpoint derives `status` from a request flag, and `build_ctrader_deployment_plan`
    also defaults to `PAUSED`; the `status in {ACTIVE, PAUSED, STOPPED}` guard is what
    makes an unexpected value fail closed instead of reaching `LiveDeploymentConfig`.

    Asserted for a bulk deploy because these rows are created in a loop and armed
    deployments trade real money.
    """
    for bogus in ("active", "RUNNING", "", "PAUSE", None, "  "):
        deployments = build_ctrader_deployment_plan(
            backtests=[_backtest()],
            accounts=[LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")],
            limit=5,
            status=bogus,
        )
        assert [d.status for d in deployments] == ["PAUSED"], (
            f"status={bogus!r} was planned as itself instead of failing closed"
        )


def test_the_default_status_is_paused_so_a_omitted_argument_cannot_arm() -> None:
    """`status` is keyword-only with a default, and the default must not be ACTIVE.

    The single production caller always passes it, so dropping the default's value to
    ACTIVE is invisible there - but the default is what a new caller inherits, and it
    would inherit a live deployment.
    """
    deployments = build_ctrader_deployment_plan(
        backtests=[_backtest()],
        accounts=[LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")],
        limit=5,
    )
    assert [d.status for d in deployments] == ["PAUSED"]


def test_no_accounts_means_no_plan() -> None:
    """The empty guard returns a list the caller turns into a 400.

    Dropping it left every test green because every existing plan test passes at least
    one account. Without the guard the loop below indexes `ctrader_accounts[0]` and
    raises `IndexError`, which the endpoint would surface as a 500 rather than the
    "no eligible cTrader account" 400 the operator needs.
    """
    assert (
        build_ctrader_deployment_plan(
            backtests=[_backtest()],
            accounts=[],
            limit=5,
            status="PAUSED",
        )
        == []
    )
    assert (
        build_ctrader_deployment_plan(
            backtests=[_backtest()],
            accounts=[LiveAccountConfig(id="alp-1", broker="alpaca", name="Alpaca")],
            limit=5,
            status="PAUSED",
        )
        == []
    )


def test_the_plan_forwards_the_oos_requirement_rather_than_overriding_it() -> None:
    """`require_oos_evidence` reaches `top_backtests_for_ctrader` from the caller.

    The existing OOS test pins `top_backtests_for_ctrader` directly, so the plan
    hard-coding `False` - admitting in-sample-only rows - was invisible. This is the
    caller's intent being forwarded, and C5's whole claim: in-sample-only rows have no
    business on a live shortlist.
    """
    accounts = [LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")]
    in_sample_only = _backtest(id="no-oos", oos_status="missing")

    assert build_ctrader_deployment_plan(
        backtests=[in_sample_only], accounts=accounts, limit=5, status="PAUSED"
    ) == []

    opted_out = build_ctrader_deployment_plan(
        backtests=[in_sample_only],
        accounts=accounts,
        limit=5,
        status="PAUSED",
        require_oos_evidence=False,
    )
    assert [d.signal_config["source_backtest_id"] for d in opted_out] == ["no-oos"]


def test_score_is_unit_normalized() -> None:
    """C5: a huge raw CAGR must not dominate Sharpe/PSR/drawdown."""
    from live_runner.ctrader_deployment import score_backtest_for_live

    modest = {
        "sharpe_ratio": 1.5,
        "cagr": 0.2,
        "psr": 0.8,
        "max_drawdown": -6,
        "trade_count": 12,
    }
    # 0.5 + 0.2 + 0.8 - 0.06
    assert score_backtest_for_live(modest) == pytest.approx(
        1.5 / 3.0 + 0.2 + 0.8 - 0.06
    )
