from shared.models import LiveAccountConfig

import pytest

from live_runner.ctrader_deployment import build_ctrader_deployment_plan, top_backtests_for_ctrader


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
