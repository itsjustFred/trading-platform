from pathlib import Path

import pytest

from validation.statistics.cross_platform import build_comparison_matrix


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_cross_platform_matrix_includes_reference_metrics():
    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
    )

    rows = {row["case_id"]: row for row in matrix["rows"]}
    assert set(matrix["metrics"]) == {
        "trade_count",
        "win_rate",
        "net_profit",
        "cagr",
        "sharpe_ratio",
    }
    assert rows["intraday_volatility_breakout_sp500_m1"]["reference"]["trade_count"] == 512
    assert rows["intraday_volatility_breakout_sp500_m1"]["reference"]["win_rate"] == pytest.approx(0.4492)
    assert rows["intraday_volatility_breakout_sp500_m1"]["reference"]["cagr"] is not None
    assert rows["risk_parity_quantconnect"]["reference"]["sharpe_ratio"] == 0.544687


def test_cross_platform_matrix_marks_missing_platform_artifact(tmp_path):
    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=tmp_path,
    )

    assert all(row["missing_artifacts"] for row in matrix["rows"])
    assert all(row["platform"]["sharpe_ratio"] is None for row in matrix["rows"])
