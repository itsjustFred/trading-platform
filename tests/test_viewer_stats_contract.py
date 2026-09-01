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
