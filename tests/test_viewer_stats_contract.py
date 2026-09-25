"""Cleanup 18-C slice 1 — stats payload contract.

The `/api/backtest/{id}/stats` payload must report the real best/worst closed
trade and expose whole-backtest winners/losers counts instead of silently
labeling the average winner/loser as an extreme.
"""

from __future__ import annotations

import sys
from pathlib import Path

from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit

sys.path.insert(0, str(Path(__file__).parent.parent))


def _trade(trade_id: int, pnl: float) -> Trade:
    return Trade(
        id=trade_id,
        symbol="X",
        direction="LONG",
        entry_time="2021-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=10,
        exits=[
            TradeExit(
                time="2021-01-02T00:00:00Z",
                price=100 + pnl / 10,
                quantity=10,
                pnl=pnl,
            )
        ],
        pnl=pnl,
    )


def _result(trades: list[Trade]) -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id="stats-demo",
            created_at="2026-01-01T00:00:00Z",
            engine="vectorbt",
            strategies=["Demo"],
            backtest_start="2021-01-01",
            backtest_end="2021-02-01",
            initial_capital=100000.0,
        ),
        trades=trades,
    )


def _trades() -> list[Trade]:
    return [
        _trade(1, 250.0),
        _trade(2, -80.0),
        _trade(3, 40.0),
        _trade(4, -300.0),
        _trade(5, 90.0),
    ]


def test_backtest_info_payload_exposes_period_and_data_provenance():
    from viewer import core

    result = _result([])
    result.meta.duration_days = 31
    result.meta.total_bars = 744
    result.meta.data = {
        "timeframes": ["1h"],
        "source": "dukascopy",
        "requested_data_source": "metals",
        "asset_classes": ["metals"],
        "data_files": ["/tmp/XAUUSD/1h.parquet"],
        "pnl_basis": {"pnl": "gross", "net_pnl": "net_after_fees"},
        "risk_config": {"risk_per_trade_pct": 0.01},
    }

    payload = core._backtest_info_payload(result)

    assert payload["duration_days"] == 31
    assert payload["total_bars"] == 744
    assert payload["data_source"] == "dukascopy"
    assert payload["requested_data_source"] == "metals"
    assert payload["asset_classes"] == ["metals"]
    assert payload["data_files"] == ["/tmp/XAUUSD/1h.parquet"]
    assert payload["pnl_basis"] == {"pnl": "gross", "net_pnl": "net_after_fees"}
    assert payload["risk_config"] == {"risk_per_trade_pct": 0.01}


def test_core_stats_payload_reports_exact_extremes():
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    assert payload["best_trade"] == 250.0
    assert payload["worst_trade"] == -300.0
    # Averages must remain available under their own names.
    assert payload["avg_winner"] == round((250.0 + 40.0 + 90.0) / 3, 2)
    assert payload["avg_loser"] == round((-80.0 + -300.0) / 2, 2)


def test_core_stats_payload_exposes_whole_backtest_winners_and_losers():
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    assert payload["winners"] == 3
    assert payload["losers"] == 2
    assert payload["total_trades"] == 5


def test_main_stats_endpoint_serves_exact_extremes(viewer_client, tmp_path):
    import viewer.main as viewer_app_module

    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _result(_trades()).save(backtests_dir / "stats-demo.trades.json")

    resp = viewer_client.get("/api/backtest/stats-demo/stats")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["best_trade"] == 250.0
    assert payload["worst_trade"] == -300.0
    assert payload["winners"] == 3
    assert payload["losers"] == 2


def test_core_stats_payload_handles_empty_trade_list():
    from viewer import core

    payload = core._stats_payload_from_result(_result([]))

    assert payload["best_trade"] == 0
    assert payload["worst_trade"] == 0
    assert payload["winners"] == 0
    assert payload["losers"] == 0


def test_core_stats_payload_expectancy_equals_average_trade():
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    assert payload["expectancy"] == payload["avg_trade_pnl"]
    assert payload["expectancy"] == 0.0


def test_core_stats_payload_reports_total_return_in_percent_points():
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    assert payload["total_return"] == 0.0
    assert payload["total_return"] == payload["net_profit_pct"]


def test_core_stats_payload_uses_net_trade_values_after_fees():
    from viewer import core

    trade = _trade(1, 100.0)
    trade.fees = 10.0
    trade.net_pnl = 90.0

    payload = core._stats_payload_from_result(_result([trade]))

    assert payload["best_trade"] == 90.0
    assert payload["net_profit"] == 90.0
    assert payload["avg_winner"] == 90.0


def test_stats_payload_percent_scale_matches_frontend_contract():
    """The frontend `PERCENT_METRIC_SCALES` table claims specific units for each
    percent-like key. This pins the backend half of that contract so a producer
    cannot silently flip between fraction (0..1) and percent points (0..100)."""
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    # "points"-scaled keys in frontend/src/api/metricScale.ts.
    for key in ("net_profit_pct", "total_return", "win_rate", "max_drawdown_pct", "cagr"):
        value = payload[key]
        assert value is None or -100.0 <= value <= 100.0, (
            f"{key}={value!r} is not in percent points (-100..100); "
            "update frontend/src/api/metricScale.ts if the unit changed"
        )
    # "fraction"-scaled keys: probabilities and bounded ratios in 0..1.
    for key in ("psr",):
        value = payload[key]
        assert value is None or 0.0 <= value <= 1.0, (
            f"{key}={value!r} is not a fraction (0..1); "
            "update frontend/src/api/metricScale.ts if the unit changed"
        )
