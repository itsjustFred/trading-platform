import json
from pathlib import Path

import pytest

from validation.statistics.cross_platform import build_comparison_matrix
from validation.importers.mt5_xlsx import MT5XLSXImporter
from scripts.regenerate_mt5_parity_artifacts import COMPARISON_CASES
from scripts.regenerate_qc_risk_parity_artifact import DEFAULT_OUTPUT as QC_RISK_PARITY_OUTPUT
from shared.models import BacktestMeta, BacktestResult, EquityPoint


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
    # The reference was regenerated with the LEAN-convention stats
    # (A-H3/A-L3/A-L4: calendar-day returns, risk-free-rate subtraction —
    # see the raw_metadata note in risk_parity_reference.json); this pins
    # the regenerated value, not the pre-A-M6 0.544687.
    assert rows["risk_parity_quantconnect"]["reference"]["sharpe_ratio"] == 0.213296
    assert rows["risk_parity_quantconnect"]["platform_path"].endswith(
        "data/backtests/risk_parity_quantconnect_reimplementation.json"
    )
    for row in rows.values():
        assert Path(row["reference_path"]).resolve().is_relative_to(PROJECT_ROOT)
        assert Path(row["platform_path"]).resolve().is_relative_to(PROJECT_ROOT)


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


def test_cross_platform_matrix_recomputes_sharpe_with_reference_risk_free_rate(tmp_path):
    equity = [EquityPoint(time="2024-01-01T00:00:00+00:00", equity=100000.0)]
    value = 100000.0
    for day in range(1, 31):
        value *= 1 + (0.00012 + (0.00004 if day % 2 else -0.00003))
        equity.append(EquityPoint(time=f"2024-01-{day + 1:02d}T00:00:00+00:00", equity=value))

    result = BacktestResult(
        meta=BacktestMeta(
            id="intraday_volatility_breakout_sp500_m1",
            created_at="2026-07-09T00:00:00+00:00",
            engine="native",
            strategies=["intraday_volatility_breakout"],
            initial_capital=100000.0,
        ),
        trades=[],
        equity_curve=equity,
        stats={"sharpe_ratio": -1.0},
    )
    result.save(tmp_path / "intraday_volatility_breakout_sp500_m1.json")

    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
        platform_data_dir=tmp_path,
    )

    rows = {row["case_id"]: row for row in matrix["rows"]}
    assert rows["intraday_volatility_breakout_sp500_m1"]["platform"]["sharpe_ratio"] > 0.0
    assert "risk_free_rate=0.0" in matrix["risk_free_rate_note"]


def test_cross_platform_matrix_loads_mt5_reference_timestamps_in_report_timezone():
    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
    )
    row = {
        item["case_id"]: item
        for item in matrix["rows"]
    }["intraday_volatility_breakout_sp500_m1"]

    assert row["reference_source_timezone"] == "Europe/Athens"
    reference = MT5XLSXImporter().load(
        Path(row["reference_path"]),
        source_timezone=row["reference_source_timezone"],
    )
    platform = json.loads(Path(row["platform_path"]).read_text(encoding="utf-8"))

    first_reference_entry = reference.trades[0].entry_time
    first_platform_entry = platform["trades"][0]["entry_time"].replace("+00:00", "Z")
    assert first_reference_entry == first_platform_entry == "2018-01-16T14:45:00Z"


def test_cross_platform_matrix_uses_qc_rebalance_attribution_stats(tmp_path):
    result = BacktestResult(
        meta=BacktestMeta(
            id="risk_parity_quantconnect_reimplementation",
            created_at="2026-07-12T00:00:00+00:00",
            engine="python-reimplementation",
            strategies=["RiskParityStrategy"],
            tags=["qc-parity", "risk-parity"],
            initial_capital=1_000_000.0,
        ),
        trades=[],
        equity_curve=[],
        stats={
            "total_trades": 3,
            "win_rate": 0.1,
            "net_profit": 999.0,
            "cagr": 0.2,
            "sharpe_ratio": 0.3,
            "rebalance_attribution_win_rate": 0.75,
            "rebalance_attribution_net_profit": 123.45,
        },
    )
    result.save(tmp_path / "risk_parity_quantconnect_reimplementation.json")

    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
        platform_data_dir=tmp_path,
    )

    rows = {row["case_id"]: row for row in matrix["rows"]}
    platform = rows["risk_parity_quantconnect"]["platform"]
    assert platform["win_rate"] == pytest.approx(0.75)
    assert platform["net_profit"] == pytest.approx(123.45)
    assert platform["cagr"] == pytest.approx(0.2)
    assert platform["sharpe_ratio"] == pytest.approx(0.3)


def test_cross_platform_matrix_ignores_malformed_qc_attribution_stats(tmp_path):
    result = BacktestResult(
        meta=BacktestMeta(
            id="risk_parity_quantconnect_reimplementation",
            created_at="2026-07-12T00:00:00+00:00",
            engine="python-reimplementation",
            strategies=["RiskParityStrategy"],
            tags=["qc-parity", "risk-parity"],
            initial_capital=1_000_000.0,
        ),
        trades=[],
        equity_curve=[],
        stats={
            "total_trades": 3,
            "win_rate": 0.1,
            "net_profit": 999.0,
            "cagr": 0.2,
            "sharpe_ratio": 0.3,
            "rebalance_attribution_win_rate": "not-a-number",
            "rebalance_attribution_net_profit": "not-a-number",
        },
    )
    result.save(tmp_path / "risk_parity_quantconnect_reimplementation.json")

    matrix = build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
        platform_data_dir=tmp_path,
    )

    rows = {row["case_id"]: row for row in matrix["rows"]}
    platform = rows["risk_parity_quantconnect"]["platform"]
    assert platform["win_rate"] == pytest.approx(0.1)
    assert platform["net_profit"] == pytest.approx(999.0)


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
