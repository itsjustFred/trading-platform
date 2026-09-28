"""Cleanup 18-C slice 1 — stats payload contract.

The `/api/backtest/{id}/stats` payload must report the real best/worst closed
trade and expose whole-backtest winners/losers counts instead of silently
labeling the average winner/loser as an extreme.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _net_gaining_trades() -> list[Trade]:
    """+3.125% on 100k of capital: 0.03125 as a fraction, 3.125 as points."""
    return [
        _trade(1, 2000.0),
        _trade(2, -500.0),
        _trade(3, 900.0),
        _trade(4, -275.0),
        _trade(5, 1000.0),
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


def test_core_stats_payload_reports_total_return_as_a_fraction():
    """`total_return` is the engine's fraction; `net_profit_pct` stays in points.

    The two keys describe the same quantity, so the payload must serve them a
    hundred to one apart. This test used to assert they were EQUAL, which was
    only true because `total_return` was being emitted in percent points; see
    tests/test_metric_scale_contract.py for the cross-language half.
    """
    from viewer import core

    payload = core._stats_payload_from_result(_result(_trades()))

    assert payload["total_return"] == 0.0
    assert payload["total_return"] * 100 == pytest.approx(payload["net_profit_pct"])


def test_core_stats_payload_reports_a_non_zero_total_return_as_a_fraction():
    """The zero case above cannot tell 0.0 (fraction) from 0.0 (points)."""
    from viewer import core

    result = _result(_net_gaining_trades())
    result.equity_curve = [
        core.EquityPoint(**point) for point in core._equity_curve_from_trades(result)
    ]

    payload = core._stats_payload_from_result(result)

    # +3.125% on 100k of capital: 0.03125 as a fraction, 3.125 as points.
    assert payload["net_profit_pct"] == pytest.approx(3.12)
    assert payload["total_return"] == pytest.approx(0.03125)
    assert abs(payload["total_return"]) <= 1.0, (
        "a fraction can never exceed 1.0 in magnitude; a points value of 3.12 "
        "here is the bug this assertion exists to catch"
    )


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
    cannot silently flip between fraction (0..1) and percent points (0..100).

    A `-100..100` bound proves nothing about the unit — it is satisfied by a
    fraction of 1.5 and by percent points of 12.5 alike — so each key is checked
    against the EXACT value the engine produced, converted into the unit the
    table declares. tests/test_metric_scale_contract.py is the cross-language
    half: it reads the table itself instead of a copy of these keys.
    """
    from runner.stats import compute_stats_for_result
    from viewer import core

    result = _result(_net_gaining_trades())
    result.equity_curve = [
        core.EquityPoint(**point) for point in core._equity_curve_from_trades(result)
    ]
    canonical = compute_stats_for_result(result)
    net_pnl = sum(trade.pnl for trade in result.trades)

    payload = core._stats_payload_from_result(result)

    # Canonical engine fractions, in the same order as metricScale.ts.
    fractions = {
        "net_profit_pct": net_pnl / result.meta.initial_capital,
        "win_rate": 3 / 5,  # three of the five trades closed positive
        "max_drawdown_pct": canonical["max_drawdown_pct"],
        "cagr": canonical["cagr"],
        "total_return": canonical["total_return"],
    }
    # metricScale.ts declares these percent-like keys in percent points.
    points_keys = {"net_profit_pct", "win_rate", "max_drawdown_pct", "cagr"}
    # The producer rounds each key; compare at that published precision.
    places = {
        "net_profit_pct": 2,
        "win_rate": 1,
        "max_drawdown_pct": 2,
        "cagr": 2,
        "total_return": 6,
    }

    for key, fraction in fractions.items():
        scale = 100.0 if key in points_keys else 1.0
        tolerance = 0.5 * 10.0**-places[key] + 0.5 * 10.0**-6 + 1e-12
        assert payload[key] == pytest.approx(fraction * scale, abs=tolerance), (
            f"{key}={payload[key]!r} is not the canonical {fraction!r} in the "
            "unit metricScale.ts declares; update "
            "frontend/src/api/metricScale.ts if the unit changed"
        )
    # `psr` is only defined once the equity curve spans enough days, which this
    # one-trade-per-day fixture does not reach. For a probability the range IS
    # the discriminating check: 12.5 is not a PSR in either unit.
    assert payload["psr"] is None or 0.0 <= payload["psr"] <= 1.0, (
        f"psr={payload['psr']!r} is not a fraction (0..1); "
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


def test_service_payload_distinguishes_idle_from_dead():
    """`status` alone cannot tell a live-but-idle service from a dead one.

    That ambiguity is the whole reason the daemon heartbeats. This pins the two
    fields that resolve it, including the naive/aware comparison that a stored
    naive-UTC timestamp would otherwise blow up on.
    """
    from datetime import datetime, timedelta, timezone

    from shared.models import Service, ServiceStatus, ServiceType
    from viewer.main import SERVICE_STALE_AFTER_SECONDS, _serialize_service_payload

    now = datetime.now(timezone.utc)

    def _service(last_activity):
        return Service(
            id="11111111-1111-1111-1111-111111111111",
            name="trading-daemon",
            type=ServiceType.TRADING_DAEMON,
            status=ServiceStatus.RUNNING,
            config={},
            last_activity=last_activity,
            created_at=now,
        )

    fresh = _serialize_service_payload(_service(now))
    assert fresh["is_stale"] is False
    assert fresh["last_activity_age_seconds"] < SERVICE_STALE_AFTER_SECONDS
    # A heartbeat that is merely old is still a heartbeat.
    assert fresh["status"] == "running"

    old = _serialize_service_payload(
        _service(now - timedelta(seconds=SERVICE_STALE_AFTER_SECONDS + 60))
    )
    assert old["is_stale"] is True
    assert old["last_activity_age_seconds"] > SERVICE_STALE_AFTER_SECONDS

    # Never reported: the worst case, and it must not be silently "fresh".
    never = _serialize_service_payload(_service(None))
    assert never["is_stale"] is True
    assert never["last_activity_age_seconds"] is None

    # Stored timestamps are naive UTC; comparing them to an aware now used to
    # raise TypeError and take the whole /api/services response down.
    naive = _serialize_service_payload(
        _service(datetime.now(timezone.utc).replace(tzinfo=None))
    )
    assert naive["is_stale"] is False


def test_live_control_audit_endpoint_is_read_only(viewer_client, isolated_db):
    """The audit trail is only worth anything if it cannot be edited.

    There is deliberately no write, update or delete route: a trail that can be
    rewritten is not a trail.
    """
    import shared.database as database

    database.record_live_control_audit(
        action="deployment.status",
        target_type="live_deployment",
        target_id="dep-audit",
        actor="operator@example",
        detail={"requested_status": "ACTIVE"},
    )

    response = viewer_client.get("/api/live/audit?target_id=dep-audit")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["action"] == "deployment.status"
    assert rows[0]["actor"] == "operator@example"
    assert rows[0]["detail"]["requested_status"] == "ACTIVE"

    for method, path in (
        ("post", "/api/live/audit"),
        ("put", "/api/live/audit"),
        ("delete", "/api/live/audit"),
        ("post", f"/api/live/audit/{rows[0]['id']}"),
        ("delete", f"/api/live/audit/{rows[0]['id']}"),
    ):
        assert viewer_client.request(method, path).status_code in {404, 405}, (
            f"{method.upper()} {path} must not exist: the audit trail is read-only"
        )


def test_actor_is_attribution_by_claim_not_invented(viewer_client):
    """No auth on this platform, so the operator is self-declared.

    Recorded rather than left blank: an unattributed change to a live
    deployment is worse than a self-declared one, and a wrong answer is visible.
    """
    from viewer.main import _live_control_actor

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    assert _live_control_actor(_Req({"X-Operator": "fred"})) == "fred"
    assert _live_control_actor(_Req({})) == "local-operator"
    assert _live_control_actor(_Req({"X-Operator": "   "})) == "local-operator"
    assert _live_control_actor(None) == "local-operator"
