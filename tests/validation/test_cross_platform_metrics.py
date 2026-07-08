from pathlib import Path

import pytest

from validation.statistics.cross_platform import build_comparison_matrix
from scripts.regenerate_mt5_parity_artifacts import COMPARISON_CASES
from scripts.regenerate_qc_risk_parity_artifact import DEFAULT_OUTPUT as QC_RISK_PARITY_OUTPUT


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
    assert rows["intraday_volatility_breakout_sp500_m1"]["platform_path"].endswith(
        "data/backtests/intraday_volatility_breakout_sp500_m1.json"
    )
    assert rows["intraday_volatility_breakout_eurusd_h1"]["platform_path"].endswith(
        "data/backtests/intraday_volatility_breakout_eurusd_h1.json"
    )
    assert rows["intraday_volatility_breakout_nas100_m15"]["platform_path"].endswith(
        "data/backtests/intraday_volatility_breakout_nas100_m15.json"
    )
    assert rows["risk_parity_quantconnect"]["reference"]["sharpe_ratio"] == 0.544687
    assert rows["risk_parity_quantconnect"]["platform_path"].endswith(
        "data/backtests/risk_parity_quantconnect_reimplementation.json"
    )


def test_cross_platform_matrix_marks_missing_platform_artifact(tmp_path):
    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=tmp_path,
    )

    assert all(row["missing_artifacts"] for row in matrix["rows"])
    assert all(row["platform"]["sharpe_ratio"] is None for row in matrix["rows"])


def test_cross_platform_matrix_honors_explicit_platform_data_dir(tmp_path):
    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
        platform_data_dir=tmp_path,
    )

    rows = {row["case_id"]: row for row in matrix["rows"]}
    assert rows["intraday_volatility_breakout_sp500_m1"]["platform_path"] == str(
        tmp_path.resolve() / "intraday_volatility_breakout_sp500_m1.json"
    )
    assert rows["risk_parity_quantconnect"]["platform_path"] == str(
        tmp_path.resolve() / "risk_parity_quantconnect_reimplementation.json"
    )


def test_regenerator_outputs_match_cross_platform_matrix_paths():
    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
    )
    rows = {row["case_id"]: Path(row["platform_path"]).resolve() for row in matrix["rows"]}

    expected_mt5 = {Path(case["output"]).resolve() for case in COMPARISON_CASES}
    actual_mt5 = {
        rows["intraday_volatility_breakout_sp500_m1"],
        rows["intraday_volatility_breakout_eurusd_h1"],
        rows["intraday_volatility_breakout_nas100_m15"],
    }
    assert actual_mt5 == expected_mt5
    assert rows["risk_parity_quantconnect"] == QC_RISK_PARITY_OUTPUT.resolve()
