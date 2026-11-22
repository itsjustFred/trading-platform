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
            "max_drawdown": -0.05,
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
            "max_drawdown": -0.08,
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
            "max_drawdown": -0.06,
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


def test_build_ctrader_deployment_plan_never_activates_an_unknown_status() -> None:
    """An unrecognised deployment status must never read as ACTIVE.

    The planner writes only one of the three states the daemon dispatches.
    Anything else - a typo, a future state, a differently-spelled ACTIVE -
    falls back to PAUSED, so the plan exists but is not traded until a human
    maps it. Reading it as ACTIVE would make a typo trade live money.
    """
    backtests = [
        {
            "id": "bt-1",
            "strategy_name": "fx_breakout",
            "symbols": ["EURUSD"],
            "timeframe": "15m",
            "sharpe_ratio": 1.5,
            "cagr": 0.2,
            "psr": 0.8,
            "max_drawdown": -0.06,
            "trade_count": 12,
            "oos_status": "available",
        }
    ]
    accounts = [LiveAccountConfig(id="ct-1", broker="ctrader", name="cTrader")]

    for unknown in ("LIVE", "active", "", "ACTIVE ", "Stopped", "Pending"):
        deployments = build_ctrader_deployment_plan(
            backtests=backtests,
            accounts=accounts,
            limit=5,
            status=unknown,
        )

        assert len(deployments) == 1
        assert deployments[0].status == "PAUSED", unknown

    # The three real states still pass through unchanged - the fallback only
    # catches what the daemon cannot dispatch.
    for known in ("ACTIVE", "PAUSED", "STOPPED"):
        deployments = build_ctrader_deployment_plan(
            backtests=backtests,
            accounts=accounts,
            limit=5,
            status=known,
        )

        assert deployments[0].status == known


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
            "max_drawdown": -0.01,
            "trade_count": 100,
        },
    ]
    assert top_backtests_for_ctrader(rows, limit=5) == []
    assert len(top_backtests_for_ctrader(rows, limit=5, require_oos_evidence=False)) == 1


def test_score_is_unit_normalized() -> None:
    """C5: a huge raw CAGR must not dominate Sharpe/PSR/drawdown."""
    from live_runner.ctrader_deployment import score_backtest_for_live

    modest = {
        "sharpe_ratio": 1.5,
        "cagr": 0.2,
        "psr": 0.8,
        "max_drawdown": -0.06,
        "trade_count": 12,
    }
    # 0.5 + 0.2 + 0.8 - 0.06
    assert score_backtest_for_live(modest) == pytest.approx(
        1.5 / 3.0 + 0.2 + 0.8 - 0.06
    )


def test_drawdown_penalty_reads_max_drawdown_as_fraction() -> None:
    """Payload `max_drawdown` is a fraction (-0.25 = 25 %), not a percentage.

    The penalty must be the fraction itself (0..1). Dividing by 100 made a
    25 % drawdown cost 0.0025 instead of 0.25, so the live shortlist was
    effectively blind to drawdown.
    """
    from live_runner.ctrader_deployment import score_backtest_for_live

    base = {"sharpe_ratio": 1.5, "cagr": 0.2, "psr": 0.8, "trade_count": 12}
    smooth = score_backtest_for_live({**base, "max_drawdown": -0.05})
    deep = score_backtest_for_live({**base, "max_drawdown": -0.25})

    assert deep == pytest.approx(1.5 / 3.0 + 0.2 + 0.8 - 0.25)
    assert smooth - deep == pytest.approx(0.20)


def test_drawdown_penalty_is_capped_at_one() -> None:
    """A drawdown of 100 % or more costs at most 1.0 (documented 0..1 penalty)."""
    from live_runner.ctrader_deployment import score_backtest_for_live

    base = {"sharpe_ratio": 1.5, "cagr": 0.2, "psr": 0.8, "trade_count": 12}
    total = score_backtest_for_live({**base, "max_drawdown": -1.0})
    beyond = score_backtest_for_live({**base, "max_drawdown": -3.0})

    assert total == pytest.approx(1.5 / 3.0 + 0.2 + 0.8 - 1.0)
    assert beyond == pytest.approx(total)
