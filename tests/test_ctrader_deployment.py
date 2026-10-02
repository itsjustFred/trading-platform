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


class TestOutOfSampleEvidenceGate:
    """Whether a backtest row is allowed onto a live shortlist at all.

    Two mutations survived, and both are about the *boundary* rather than the shape.

    **`oos_sharpe` must be strictly positive.** Zero is not evidence of anything -
    an out-of-sample result of exactly 0.0 is a coin flip, and the docstring's rule is
    "in-sample-only rows have no business on a live shortlist". Widening `> 0` to `>= 0`
    therefore admits every flat out-of-sample run, which is the single largest population
    of a strategy sweep. The asymmetry is easy to miss because a row with *no* evidence
    also evaluates falsy, so `> 0` and `>= 0` look equivalent on the empty case.

    **An explicit `oos_status == "available"` counts on its own.** The optimizer uses that
    vocabulary rather than a Sharpe, so requiring a number would reject rows that carry
    perfectly good evidence in the other form. Both routes are asserted separately, and
    the fallback is asserted too: a *malformed* `oos_sharpe` does not raise, it falls
    through to the status check. A row with `"oos_sharpe": "n/a"` and
    `"oos_status": "available"` is evidence; the same row with no status is not.
    """

    def test_a_positive_oos_sharpe_is_evidence(self) -> None:
        from live_runner.ctrader_deployment import _has_oos_evidence

        assert _has_oos_evidence({"oos_sharpe": 1.5}) is True
        assert _has_oos_evidence({"oos_sharpe": 0.01}) is True
        # A numeric string, which is how a CSV-loaded row arrives.
        assert _has_oos_evidence({"oos_sharpe": "2.0"}) is True

    def test_a_zero_or_negative_oos_sharpe_is_not_evidence(self) -> None:
        """**The surviving mutation.** `>= 0` would admit every flat out-of-sample run.

        Zero is a coin flip, not evidence, and a flat result is the largest single
        population of a strategy sweep - so the boundary decides whether half the sweep
        reaches a live shortlist.
        """
        from live_runner.ctrader_deployment import _has_oos_evidence

        for value in (0, 0.0, "0", "0.0", -0.0, -1, -0.001, "-2.5"):
            assert _has_oos_evidence({"oos_sharpe": value}) is False, value

    def test_an_explicit_available_status_is_evidence_on_its_own(self) -> None:
        """The optimizer's vocabulary, so a number is not required."""
        from live_runner.ctrader_deployment import _has_oos_evidence

        for status in ("available", "AVAILABLE", "  Available  "):
            assert _has_oos_evidence({"oos_status": status}) is True, status
        # And it overrides a missing/zero Sharpe, because it is the positive statement.
        assert _has_oos_evidence({"oos_sharpe": 0.0, "oos_status": "available"}) is True

    def test_a_row_with_no_out_of_sample_evidence_is_refused(self) -> None:
        """In-sample-only rows have no business on a live shortlist."""
        from live_runner.ctrader_deployment import _has_oos_evidence

        for row in ({}, {"oos_sharpe": None}, {"oos_status": ""}, {"oos_status": "pending"},
                    {"oos_status": "unavailable"}, {"oos_sharpe": 0.0, "oos_status": "none"},
                    {"oos_sharpe": 3.0, "oos_status": "unavailable"}):
            # The last row is the one worth stating: a positive in-sample Sharpe with an
            # explicit "unavailable" out-of-sample status is NOT evidence, because the
            # status says the number is not out-of-sample. The two are not interchangeable.
            assert _has_oos_evidence(row) is (row.get("oos_sharpe") == 3.0), row

    def test_a_malformed_oos_sharpe_falls_through_to_the_status_check(self) -> None:
        """Not a raise: a bad number is an unknown, and the status may still answer."""
        from live_runner.ctrader_deployment import _has_oos_evidence

        assert _has_oos_evidence({"oos_sharpe": "n/a"}) is False
        assert _has_oos_evidence({"oos_sharpe": "n/a", "oos_status": "available"}) is True
        assert _has_oos_evidence({"oos_sharpe": None, "oos_status": "available"}) is True
