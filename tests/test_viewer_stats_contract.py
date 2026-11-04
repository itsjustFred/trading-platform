"""Cleanup 18-C slice 1 — stats payload contract.

The `/api/backtest/{id}/stats` payload must report the real best/worst closed
trade and expose whole-backtest winners/losers counts instead of silently
labeling the average winner/loser as an extreme.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_stats_payload_percent_scale_matches_frontend_contract():
    """The frontend `PERCENT_METRIC_SCALES` table claims specific units for each
    percent-like key. This pins the backend half of that contract so a producer
    cannot silently flip between fraction (0..1) and percent points (0..100)."""
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    # "points"-scaled keys in frontend/src/api/metricScale.ts.
    for key in ("net_profit_pct", "win_rate", "max_drawdown_pct", "cagr"):
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


def test_stats_payload_uses_net_pnl_when_fees_are_recorded():
    from viewer import core

    trade = Trade(
        id=1,
        symbol="X",
        direction="LONG",
        entry_time="2021-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=10,
        exits=[
            TradeExit(
                time="2021-01-02T00:00:00Z",
                price=110.0,
                quantity=10,
                pnl=100.0,
            )
        ],
        pnl=100.0,
        fees=10.0,
        net_pnl=90.0,
    )

    payload = core._stats_payload_from_result(_result([trade]))

    assert payload["net_profit"] == 90.0
    assert payload["best_trade"] == 90.0
    assert core._equity_curve_from_trades(_result([trade]))[-1]["equity"] == 100090.0


def test_viewer_trade_risk_uses_realized_evidence_only():
    from viewer import core

    policy_trade = Trade(
        id=1,
        symbol="X",
        direction="LONG",
        entry_time="2021-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=1,
        exits=[],
        pnl=0.0,
        risk_amount=100.0,
        risk_percent=0.01,
        meta={"risk_math": {"risk_target_usd": 100.0, "risk_target_pct": 0.01}},
    )
    realized_trade = Trade(
        id=2,
        symbol="X",
        direction="LONG",
        entry_time="2021-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=1,
        exits=[],
        pnl=0.0,
        risk_amount=100.0,
        risk_percent=0.01,
        meta={
            "risk_math": {
                "realized_risk_amount": 75.0,
                "realized_risk_percent": 0.0075,
            }
        },
    )

    assert core._trade_to_viewer(policy_trade)["risk_amount"] is None
    realized = core._trade_to_viewer(realized_trade)
    assert realized["risk_amount"] == 75.0
    assert realized["risk_percent"] == 0.0075


def test_database_risk_fallback_preserves_policy_values():
    from shared.database import _apply_backtest_trade_risk_fallbacks

    row = SimpleNamespace(
        meta={
            "risk_math": {
                "risk_target_usd": 100.0,
                "risk_target_pct": 0.01,
                "max_position_pct": 0.05,
            }
        }
    )

    assert _apply_backtest_trade_risk_fallbacks(row, 2.0, 100.0, 0.01) == (2.0, 100.0, 0.01)


def test_database_risk_fallback_accepts_explicit_realized_values():
    from shared.database import _apply_backtest_trade_risk_fallbacks

    row = SimpleNamespace(
        meta={
            "risk_math": {
                "realized_risk_amount": 75.0,
                "equity_at_entry": 10000.0,
            }
        }
    )

    assert _apply_backtest_trade_risk_fallbacks(row, None, None, None) == (
        None,
        75.0,
        0.0075,
    )


def test_core_stats_payload_preserves_zero_net_pnl_for_flat_trade():
    from viewer import core

    trade = Trade(
        id=1,
        symbol="X",
        direction="LONG",
        entry_time="2021-01-01T00:00:00Z",
        entry_price=100.0,
        quantity=10,
        exits=[],
        pnl=100.0,
        fees=100.0,
        net_pnl=0.0,
    )
    result = _result([trade])
    result.stats = {"total_pnl": 999.0}

    payload = core._stats_payload_from_result(result)

    assert payload["net_profit"] == 0.0
    assert payload["net_profit_pct"] == 0.0
    assert payload["total_trades"] == 1
    assert payload["winners"] == 0
    assert payload["losers"] == 0


def test_portfolio_summary_preserves_zero_pnl_trade_count():
    import viewer.main as viewer_app_module

    summary = viewer_app_module._portfolio_summary_from_payload(
        {
            "account_capital": 100000.0,
            "combined_stats": {},
            "trades": [
                {
                    "entry_time": "2026-01-01T00:00:00Z",
                    "exit_time": "2026-01-02T00:00:00Z",
                    "pnl": 100.0,
                    "fees": 100.0,
                    "net_pnl": 0.0,
                }
            ],
        }
    )

    assert summary["trade_count"] == 1
    assert summary["net_profit"] == 0.0
    assert summary["total_return"] == 0.0
    assert summary["win_rate"] == 0.0


def test_optimization_summary_preserves_zero_oos_baseline():
    from viewer import core

    summary = core._optimization_summary_payload(
        {
            "id": "opt-zero",
            "best_value": 0.0,
            "best_params": {},
            "effective_best_parameters": {},
            "train_stats": {"total_pnl": 0.0},
            "test_stats": {"total_pnl": 0.0},
            "oos_status": "available",
        },
        Path("opt-zero.json"),
    )

    assert summary["best_value"] == 0.0
    assert summary["train_stats"]["total_pnl"] == 0.0
    assert summary["test_stats"]["total_pnl"] == 0.0
    assert summary["oos_status"] == "available"


def test_backtest_summary_keeps_an_undefined_metric_absent():
    """An unmeasurable Sharpe/PSR/p-value must stay null, not become 0.

    `_safe_float(value, 0.0)` turns an undefined PSR into the worst attainable
    value and an undefined p-value into 1.0, so a run with too few daily returns
    is listed as a measured result. The list payload has to carry the absence
    through to the client, which renders it as unavailable.
    """
    import viewer.main as viewer_main_module

    summary = viewer_main_module._backtest_summary_from_meta(
        {
            "id": "undef-1",
            "symbols": ["X"],
            "timeframe": "1d",
            "initial_capital": 100000.0,
            "total_pnl": 0.0,
            "total_return": 0.0,
            "sharpe_ratio": None,
            "psr": None,
            "p_value": None,
        }
    )

    assert summary["sharpe"] is None
    assert summary["psr"] is None
    assert summary["p_value"] is None


def test_undefined_metrics_are_not_published_as_zero():
    """The database layer must tell "declared undefined" from "computed as 0"."""
    from shared.database import _stat_number_or_undefined

    assert _stat_number_or_undefined({"undefined_metrics": ["psr"]}, "psr", 0.0) is None
    # A producer that named the metric with a real zero keeps that zero.
    assert _stat_number_or_undefined({"psr": 0.0}, "psr", 0.0) == 0.0
    # And an artifact written before the convention keeps its old default.
    assert _stat_number_or_undefined({}, "psr", 0.0) == 0.0


def test_non_finite_stats_are_not_published_as_measurements():
    """NaN/Infinity in an artifact has no measurable value, so it stays absent.

    These are the same class of lie as the 0.0 default: `float("nan")` serialises
    to a bare `NaN` token that strict JSON clients reject, and `Infinity` reads as
    an impossibly good Sharpe. Both mean "could not compute", so they resolve to
    None like a named undefined metric - not to the caller's default.
    """
    from shared.database import _stat_number_or_undefined

    for value in (float("nan"), float("inf"), float("-inf"), "n/a"):
        assert _stat_number_or_undefined({"sharpe_ratio": value}, "sharpe_ratio", 0.0) is None

    # A numeric string is a real measurement written by an older producer.
    assert _stat_number_or_undefined({"sharpe_ratio": "1.25"}, "sharpe_ratio", 0.0) == 1.25


def test_the_backtest_info_payload_publishes_the_artifact_provenance():
    """`/api/backtest/{id}/info` must report where the data came from.

    The same rule as the stats: an empty `data_source` is a claim ("no source"),
    not an absence. The payload publishes what the artifact carries so the
    dashboard can show it instead of guessing, and leaves the rest empty rather
    than inventing a value.
    """
    from viewer.core import _backtest_info_payload

    result = _result([_trade(1, 10.0)])
    result.meta.data.update(
        {
            "source": "dukascopy",
            "requested_data_source": "dukascopy",
            "asset_classes": ["forex"],
            "data_files": ["EURUSD.csv"],
        }
    )

    payload = _backtest_info_payload(result)

    assert payload["data_source"] == "dukascopy"
    assert payload["requested_data_source"] == "dukascopy"
    assert payload["asset_classes"] == ["forex"]
    assert payload["data_files"] == ["EURUSD.csv"]


def test_compare_payload_keeps_an_undefined_metric_absent(monkeypatch):
    """The compare endpoint must not turn an unmeasured metric into a 0.

    `_safe_float(raw.get("sharpe_ratio"), 0.0)` published an undefined Sharpe as
    a measured zero, and an undefined p-value as the worst attainable 1.0. The
    side-by-side view then ranked a run that could not be measured. The payload
    carries null instead, and a real zero survives as zero.
    """
    import viewer.main as viewer_main_module

    result = _result([])
    result.stats = {
        "total_pnl": 0.0,
        "total_return": 0.0,
        "cagr": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "sharpe_ratio": None,
        "psr": None,
        "p_value": None,
        "undefined_metrics": ["sharpe_ratio", "psr", "p_value"],
    }
    monkeypatch.setattr(viewer_main_module, "_load_result", lambda _id: result)

    payload = viewer_main_module._backtest_compare_payload("undef-compare")

    assert payload["stats"]["sharpe_ratio"] is None
    assert payload["stats"]["psr"] is None
    assert payload["stats"]["p_value"] is None
    # A measured zero is still a measurement, not an absence.
    assert payload["stats"]["total_return"] == 0.0
    assert payload["stats"]["max_drawdown"] == 0.0


def test_compare_delta_is_absent_when_either_side_is_undefined():
    """An unknown side has no delta; 0.0 would invent one."""
    from viewer.main import _compare_stat_delta

    left = {"sharpe_ratio": 1.0, "p_value": None}
    right = {"sharpe_ratio": 2.0, "p_value": 0.04}

    assert _compare_stat_delta(left, right, "sharpe_ratio") == 1.0
    assert _compare_stat_delta(left, right, "p_value") is None
    assert _compare_stat_delta({}, right, "sharpe_ratio") is None


def test_detail_stats_payload_keeps_undefined_ratios_absent():
    """The detail card must show an em dash, not grade a failed computation."""
    from viewer import core

    result = _result(_trades())
    result.stats = {
        "total_pnl": 0.0,
        "total_return": 0.0,
        "cagr": None,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "calmar_ratio": None,
        "psr": None,
        "p_value": None,
        "undefined_metrics": [
            "cagr",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "psr",
            "p_value",
        ],
    }

    payload = core._stats_payload_from_result(result)

    assert payload["sharpe"] is None
    assert payload["sortino"] is None
    assert payload["calmar"] is None
    assert payload["cagr"] is None
    assert payload["psr"] is None
    assert payload["p_value"] is None


def test_quick_stats_keep_undefined_ratios_absent():
    """An empty backtest has no return series, so its ratios are not zero."""
    from viewer import core

    payload = core._quick_stats_and_equity([], 100000.0, "2021-01-01", "2021-02-01")

    assert payload["sharpe"] is None
    assert payload["cagr"] is None
    assert payload["p_value"] is None
    assert payload["psr"] is None
    # Zero profit and zero drawdown *are* measured facts.
    assert payload["net_profit"] == 0
    assert payload["max_drawdown_pct"] == 0
