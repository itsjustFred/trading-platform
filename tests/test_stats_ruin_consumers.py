"""#132: a ruined run's null return metrics must survive the consumers intact."""

from __future__ import annotations

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
