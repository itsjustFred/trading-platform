"""#132: a ruined run's null return metrics must survive the consumers intact."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from runner.stats import compute_stats
from shared.models import BacktestMeta, BacktestResult, BacktestStats, EquityPoint

START = datetime(2021, 1, 4, tzinfo=timezone.utc)


def _curve(equities: list[float]) -> list[EquityPoint]:
    return [
        EquityPoint(time=(START + timedelta(days=i)).isoformat(), equity=float(eq))
        for i, eq in enumerate(equities)
    ]


def test_ruined_stats_round_trip_keeps_nulls_and_flag() -> None:
    stats = compute_stats(
        [], initial_capital=100_000.0, equity_curve=_curve([100_000, 101_000, 0.0, 90_000])
    )

    restored = BacktestStats.from_dict(stats)

    assert restored.sharpe_ratio is None
    assert restored.sortino_ratio is None
    assert restored.psr is None
    assert restored.p_value is None
    assert restored.to_dict() == stats
    assert stats["ruined"] is True and "psr" in stats


def test_healthy_run_without_psr_still_omits_the_key() -> None:
    """Only a ruined run publishes an explicit null PSR; other payloads are unchanged."""
    payload = BacktestStats(psr=None).to_dict()

    assert "psr" not in payload


def _result_with_stats(stats: dict) -> BacktestResult:
    return BacktestResult(
        meta=BacktestMeta(
            id="ruin-demo",
            created_at="2026-01-01T00:00:00Z",
            engine="vectorbt",
            strategies=["Demo"],
            backtest_start="2021-01-04",
            backtest_end="2021-01-08",
            initial_capital=100000.0,
        ),
        trades=[],
        stats=stats,
    )


def test_viewer_stats_payload_carries_ruin_flag_and_null_ratios() -> None:
    from viewer import core

    stats = compute_stats(
        [], initial_capital=100_000.0, equity_curve=_curve([100_000, 101_000, 0.0, 90_000])
    )

    payload = core._stats_payload_from_result(_result_with_stats(stats))

    assert payload["ruined"] is True
    assert payload["ruin_time"] == stats["ruin_time"]
    for key in ("sharpe", "sortino", "psr", "p_value"):
        assert payload[key] is None, key


def test_viewer_stats_payload_legacy_artifact_reads_as_not_ruined() -> None:
    from viewer import core

    legacy = {
        "total_pnl": 1.0,
        "sharpe_ratio": 1.2,
        "cagr": 0.1,
        "max_drawdown_pct": -0.1,
    }

    payload = core._stats_payload_from_result(_result_with_stats(legacy))

    assert payload["ruined"] is False
    assert payload["ruin_time"] is None


def _result_with_unreadable_endpoint(stats: dict) -> BacktestResult:
    """A trade-less artifact whose stored curve ends on an unreadable point."""
    result = _result_with_stats(stats)
    result.equity_curve = [
        EquityPoint(time="2021-01-04T00:00:00+00:00", equity=100_000.0),
        EquityPoint(time="2021-01-05T00:00:00+00:00", equity=101_000.0),
        EquityPoint(time="2021-01-06T00:00:00+00:00", equity=None),
    ]
    return result


def test_viewer_stats_payload_renders_an_unreadable_endpoint_without_nan() -> None:
    """The recompute path must not hand NaN to the JSON response.

    `_canonical_stats_from_result` recomputes when an artifact lacks a canonical
    key. The terminating equity point is unreadable, so `total_pnl` and
    `max_drawdown_pct` come back null/undefined; copying them verbatim into the
    payload puts NaN in the body, which `JSONResponse` refuses (`allow_nan=False`)
    - the 500 this path exists to remove, one field later.
    """
    from viewer import core

    payload = core._stats_payload_from_result(_result_with_unreadable_endpoint({}))

    json.dumps(payload, allow_nan=False)
    assert payload["ruined"] is True
    assert payload["net_profit"] == 0.0
    assert payload["net_profit_pct"] == 0.0
    assert payload["max_drawdown_pct"] == 0.0
    assert payload["total_return"] == 0.0


def test_stats_endpoint_serves_an_unreadable_equity_artifact(
    viewer_client, tmp_path
) -> None:
    """The artifact page must load, not 500, for the case #250 fixes."""
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _result_with_unreadable_endpoint({}).save(backtests_dir / "ruin-demo.trades.json")

    resp = viewer_client.get("/api/backtest/ruin-demo/stats")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ruined"] is True
    assert payload["net_profit"] == 0.0


def test_viewer_stats_payload_survives_unusable_cached_figures() -> None:
    """A cached artifact can carry NaN/inf or an out-of-range integer stat (#250).

    `BacktestResult.to_json` calls `json.dumps` without `allow_nan=False`, so an
    artifact written by the path this PR fixes really can store `NaN`; and
    `json.loads` yields an arbitrary-precision int for `10**400`, which makes
    `float()` raise `OverflowError`. Neither is a measurement, and the JSON
    response refuses both, so the payload resolves them instead of copying them
    through.
    """
    from viewer import core

    for value in (float("nan"), float("inf"), 10**400):
        payload = core._stats_payload_from_result(
            _result_with_stats(
                {
                    "total_pnl": value,
                    "sharpe_ratio": 1.0,
                    "cagr": 0.1,
                    "max_drawdown_pct": value,
                }
            )
        )

        json.dumps(payload, allow_nan=False)
        assert payload["net_profit"] == 0.0, value
        assert payload["max_drawdown_pct"] == 0.0, value


def test_ruined_backtest_is_never_selected_for_live_deployment() -> None:
    """#132: a ruined run must not reach cTrader deployment, whatever its other metrics."""
    from live_runner.ctrader_deployment import score_backtest_for_live, top_backtests_for_ctrader

    healthy = {
        "id": "a", "symbols": ["EURUSD"], "sharpe_ratio": 0.5, "cagr": 0.05, "psr": 0.6,
        "max_drawdown": 10.0, "trade_count": 50, "oos_status": "available", "ruined": False,
    }
    ruined = {
        **healthy, "id": "b", "sharpe_ratio": None, "psr": None, "cagr": 0.9,
        "max_drawdown": 1.0, "ruined": True,
    }

    assert score_backtest_for_live(ruined) == float("-inf")
    assert [row["id"] for row in top_backtests_for_ctrader([ruined, healthy])] == ["a"]


def test_undefined_metric_row_is_unrankable_for_live_deployment() -> None:
    """A null score input (producer-undefined) must not be read as a measured 0.0."""
    from live_runner.ctrader_deployment import score_backtest_for_live, top_backtests_for_ctrader

    healthy = {
        "id": "a", "symbols": ["EURUSD"], "sharpe_ratio": -0.5, "cagr": -0.05, "psr": 0.1,
        "max_drawdown": 10.0, "trade_count": 50, "oos_status": "available", "ruined": False,
    }
    for key in ("sharpe_ratio", "cagr", "psr", "max_drawdown"):
        undefined = {**healthy, "id": "b", key: None}
        assert score_backtest_for_live(undefined) == float("-inf"), key
        assert [row["id"] for row in top_backtests_for_ctrader([undefined, healthy])] == ["a"]

    absent = {k: v for k, v in healthy.items() if k != "psr"}
    assert score_backtest_for_live(absent) != float("-inf")


def test_viewer_summary_keeps_ruined_cagr_null() -> None:
    from viewer.main import _backtest_summary_from_meta

    summary = _backtest_summary_from_meta(
        {"id": "r", "total_return": -1.0, "cagr": None, "ruined": True}
    )
    assert summary["cagr"] is None

    legacy = _backtest_summary_from_meta({"id": "l", "total_return": 0.25})
    assert legacy["cagr"] == 25.0
