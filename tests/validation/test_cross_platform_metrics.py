import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.regenerate_mt5_parity_artifacts import COMPARISON_CASES
from scripts.regenerate_qc_risk_parity_artifact import DEFAULT_OUTPUT as QC_RISK_PARITY_OUTPUT
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.statistics.cross_platform import (
    _platform_realized_sharpe_ratio,
    _reference_sharpe_ratio,
    build_comparison_matrix,
)

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
    assert rows["intraday_volatility_breakout_sp500_m1"]["reference"]["win_rate"] == pytest.approx(
        0.4492
    )
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
    # the regenerated value, not the pre-A-M6 0.544687. Re-pinned for #259
    # (2% rebalance gate on the previous close): 0.350569 -> 0.360581.
    assert rows["risk_parity_quantconnect"]["reference"]["sharpe_ratio"] == 0.360581
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
        trades=[
            _mt5_trade(f"2024-01-{day}", 400.0)
            for day in [
                "02", "03", "04", "05", "08", "09", "10", "11", "12",
                "15", "16", "17", "18", "19", "22",
            ]
        ],
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


@pytest.fixture(scope="module")
def matrix() -> dict:
    return build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
    )


def test_cross_platform_matrix_reads_mt5_reference_timestamps_on_the_broker_clock(matrix):
    row = {item["case_id"]: item for item in matrix["rows"]}[
        "intraday_volatility_breakout_sp500_m1"
    ]

    assert row["reference_broker_clock"] == "dukascopy_mt5_index"
    reference = MT5XLSXImporter().load(
        Path(row["reference_path"]),
        broker_clock=row["reference_broker_clock"],
    )
    platform_path = Path(row["platform_path"])
    if not platform_path.exists():
        pytest.skip(
            f"{platform_path.name} not present (artifacts are generated locally, not "
            "committed); run scripts/regenerate_mt5_parity_artifacts.py"
        )
    platform = json.loads(platform_path.read_text(encoding="utf-8"))
    reference_entries = {trade.entry_time for trade in reference.trades}
    platform_entries = {trade["entry_time"].replace("+00:00", "Z") for trade in platform["trades"]}

    assert reference.trades[0].entry_time == "2018-01-16T14:45:00Z"
    # Legacy index history is fixed UTC+2: Athens would read 15:00Z / 15:45Z.
    assert "2019-07-16T16:00:00Z" in reference_entries
    assert "2025-03-12T14:45:00Z" in reference_entries
    # The platform artifacts ran on the same clock, so the entries coincide.
    assert reference.trades[0].entry_time == platform["trades"][0]["entry_time"].replace(
        "+00:00", "Z"
    )
    assert {"2025-03-12T14:45:00Z", "2026-03-20T14:00:00Z"} <= platform_entries


def test_cross_platform_mt5_cases_share_the_parity_artifacts_broker_clock(matrix):
    """One clock per symbol: the audit matrix and the platform run must agree."""
    expected = {
        f"intraday_volatility_breakout_{case['label'].lower()}": case["broker_clock"]
        for case in COMPARISON_CASES
    }
    actual = {
        row["case_id"]: row["reference_broker_clock"]
        for row in matrix["rows"]
        if "reference_broker_clock" in row
    }
    assert actual == expected


def test_comparison_matrix_parses_each_mt5_report_on_its_case_broker_clock(monkeypatch):
    """The matrix's own parse uses the case clock, not only the test helpers' reloads.

    Recording the kwargs of the importer call made inside ``build_comparison_matrix``
    catches a regression to ``source_timezone="Europe/Athens"`` even when no
    matrix statistic moves.
    """
    original_load = MT5XLSXImporter.load
    recorded: dict[str, dict[str, object]] = {}

    def recording_load(self, source, *args, **kwargs):
        assert not args, "the matrix must name the clock by keyword"
        recorded[Path(source).name] = kwargs
        return original_load(self, source, *args, **kwargs)

    monkeypatch.setattr(MT5XLSXImporter, "load", recording_load)

    build_comparison_matrix(
        comparisons_dir=PROJECT_ROOT / "strategy-comparisons",
        reports_dir=PROJECT_ROOT / "reports",
    )

    assert recorded == {
        f"{case['label']}.xlsx": {"broker_clock": case["broker_clock"]} for case in COMPARISON_CASES
    }


def test_cross_platform_matrix_always_recomputes_canonical_stats(tmp_path):
    """#84: the audit matrix recomputes canonical stats — stored/curated
    numbers (and qc-parity attribution overrides) never win."""
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
    # Empty trades/equity recompute to canonical zeros — neither the stored
    # 0.1/999.0 nor the attribution 0.75/123.45 survive.
    assert platform["win_rate"] == pytest.approx(0.0)
    assert platform["net_profit"] == pytest.approx(0.0)


def test_cross_platform_matrix_ignores_malformed_qc_attribution_stats(tmp_path):
    """#84: malformed attribution stats are irrelevant — canonical wins."""
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
    assert platform["win_rate"] == pytest.approx(0.0)
    assert platform["net_profit"] == pytest.approx(0.0)


def _mt5_like_reference(trades: list[Trade], printed_sharpe: float = 1.2345):
    return SimpleNamespace(
        trades=trades,
        settings={},
        raw_metadata={"parsed_meta": {"initial_capital": 100_000.0}},
        stats={"sharpe_ratio": printed_sharpe},
    )


def _mt5_trade(exit_day: str, pnl: float) -> Trade:
    return Trade(
        id=1,
        symbol="EURUSD",
        direction="LONG",
        entry_time=f"{exit_day}T08:00:00Z",
        entry_price=1.1,
        quantity=1.0,
        exits=[
            TradeExit(
                time=f"{exit_day}T16:00:00Z",
                price=1.1,
                quantity=1.0,
                pnl=pnl,
            )
        ],
        pnl=pnl,
        net_pnl=pnl,
    )


def test_reference_sharpe_uses_platform_formula_on_imported_curve():
    """#229: the MT5 side of the parity check runs OUR formula on the
    trade-derived equity curve, not MT5's printed Sharpe-Ratio."""
    trades = [
        _mt5_trade(f"2024-01-{day:02d}", pnl)
        for day, pnl in [
            (2, 500.0),
            (3, -200.0),
            (4, 300.0),
            (5, 400.0),
            (8, -100.0),
            (9, 600.0),
            (10, 200.0),
            (11, -50.0),
            (12, 350.0),
            (15, 250.0),
        ]
    ]
    reference = _mt5_like_reference(trades)

    sharpe = _reference_sharpe_ratio(reference)

    assert sharpe is not None
    # Same-formula check, independently rebuilt: the reference curve is
    # calendar-filled (one point per calendar day, flat between exits) —
    # the shape production publishes via attach_daily_equity — and the
    # 2026-11-22 convention drops the weekend filler rows and annualises
    # at 252 for this unclassified (non-crypto) book.
    from datetime import datetime, timedelta, timezone

    from runner.stats import compute_stats
    from shared.models import EquityPoint

    pnl_by_day: dict[str, float] = {}
    for trade in trades:
        day = trade.exits[-1].time[:10]
        pnl_by_day[day] = pnl_by_day.get(day, 0.0) + float(trade.net_pnl)

    start = datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc)
    last_day = datetime(2024, 1, 15, tzinfo=timezone.utc).date()
    equity = 100_000.0
    points = [EquityPoint(time=start.isoformat(), equity=equity)]
    day = start.date()
    while day <= last_day:
        equity += pnl_by_day.get(day.isoformat(), 0.0)
        points.append(
            EquityPoint(
                time=datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc).isoformat(),
                equity=equity,
            )
        )
        day += timedelta(days=1)

    stats = compute_stats([], initial_capital=100_000.0, equity_curve=points)
    assert stats["equity_return_periods_per_year"] == pytest.approx(252.0)
    expected = stats["sharpe_ratio"]
    assert sharpe == pytest.approx(expected, abs=1e-4)
    # Trading-day filtering makes the fill style irrelevant: an exits-only
    # curve resamples to the same daily equity and the same trading-day
    # return series, so it now publishes the SAME Sharpe — the weekend
    # filler rows that used to distort it are dropped on both shapes.
    exit_only_stats = compute_stats(
        [],
        initial_capital=100_000.0,
        equity_curve=[
            EquityPoint(time=start.isoformat(), equity=100_000.0),
            *[
                EquityPoint(
                    time=t.exits[-1].time,
                    equity=100_000.0 + sum(float(x.net_pnl) for x in trades[: i + 1]),
                )
                for i, t in enumerate(trades)
            ],
        ],
    )
    assert exit_only_stats["sharpe_ratio"] == pytest.approx(sharpe, abs=1e-4)


def test_parity_sharpe_compares_realized_curves_on_identical_trade_lists(tmp_path):
    """#349 review: the platform side of the MT5 parity row must run the
    same formula on the same curve SHAPE as the reference — a
    realized-exit calendar-filled curve rebuilt from its own trade list,
    not the mark-to-market equity curve. Identical trade lists give
    identical Sharpes; the MTM value stays visible as sharpe_ratio_mtm."""
    trades = [
        _mt5_trade(f"2024-01-{day:02d}", pnl)
        for day, pnl in [
            (2, 500.0), (3, -200.0), (4, 300.0), (5, 400.0),
            (8, -100.0), (9, 600.0), (10, 200.0), (11, -50.0),
            (12, 350.0), (15, 250.0),
        ]
    ]
    platform_result = BacktestResult(
        meta=BacktestMeta(
            id="intraday_volatility_breakout_eurusd_h1",
            created_at="2026-07-09T00:00:00+00:00",
            engine="native",
            strategies=["intraday_volatility_breakout"],
            initial_capital=100_000.0,
            data={"symbols": ["EURUSD"], "source": "dukascopy"},
        ),
        trades=trades,
        equity_curve=[],
        stats={"sharpe_ratio": 9.99},
    )
    platform_path = tmp_path / "intraday_volatility_breakout_eurusd_h1.json"
    platform_result.save(platform_path)

    reference = _mt5_like_reference(trades)
    context = {
        "symbols": ["EURUSD"],
        "data_source": "dukascopy",
        "asset_class": "fx",
        "data_dir": None,
    }

    reference_sharpe = _reference_sharpe_ratio(reference, **context)
    platform_sharpe = _platform_realized_sharpe_ratio(platform_path, context)

    assert reference_sharpe is not None
    assert platform_sharpe == pytest.approx(reference_sharpe, rel=1e-9)
    # The stored/MTM Sharpe is not the comparison value.
    assert platform_sharpe != pytest.approx(9.99)


def test_mt5_row_keeps_platform_mtm_sharpe_as_a_separate_field(tmp_path):
    """The audit row keeps the platform's canonical mark-to-market Sharpe
    under ``sharpe_ratio_mtm`` while ``sharpe_ratio`` carries the
    like-for-like realized-exit recomputation."""
    trades = [
        _mt5_trade(f"2024-01-{day}", 250.0)
        for day in ["02", "03", "04", "05", "08", "09", "10", "11", "12"]
    ]
    platform_result = BacktestResult(
        meta=BacktestMeta(
            id="intraday_volatility_breakout_sp500_m1",
            created_at="2026-07-09T00:00:00+00:00",
            engine="native",
            strategies=["intraday_volatility_breakout"],
            initial_capital=100_000.0,
            data={"symbols": ["SP500"], "source": "dukascopy"},
        ),
        trades=trades,
        equity_curve=[
            EquityPoint(time=f"2024-01-{day}T23:59:59+00:00", equity=100_000.0 + i * 300.0)
            for i, day in enumerate(
                ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]
            )
        ],
        stats={"sharpe_ratio": 1.5},
    )
    platform_path = tmp_path / "intraday_volatility_breakout_sp500_m1.json"
    platform_result.save(platform_path)

    from validation.statistics.cross_platform import _build_mt5_row
    from validation.statistics.cross_platform import MT5ComparisonCase

    row = _build_mt5_row(
        MT5ComparisonCase(
            case_id="intraday_volatility_breakout_sp500_m1",
            reference_path=PROJECT_ROOT
            / "strategy-comparisons/intraday_volatility_breakout/SP500_M1.xlsx",
            platform_path=platform_path,
            broker_clock="dukascopy_mt5_index",
        )
    )

    platform = row["platform"]
    assert platform["sharpe_ratio"] is not None
    assert platform["sharpe_ratio_mtm"] is not None
    assert platform["sharpe_ratio"] != pytest.approx(platform["sharpe_ratio_mtm"])
    assert any("sharpe_ratio_mtm" in note for note in row["notes"])


def test_reference_sharpe_fails_closed_without_enough_exits():
    reference = _mt5_like_reference([_mt5_trade("2024-01-02", 100.0)])

    assert _reference_sharpe_ratio(reference) is None


def test_reference_sharpe_fails_closed_on_unparseable_exit_time():
    trade = _mt5_trade("2024-01-02", 100.0)
    trade.exits[0].time = "not-a-timestamp"
    reference = _mt5_like_reference([trade, _mt5_trade("2024-01-03", 50.0)])

    assert _reference_sharpe_ratio(reference) is None


def test_reference_sharpe_fails_closed_on_non_finite_pnl():
    trade = _mt5_trade("2024-01-02", float("nan"))
    reference = _mt5_like_reference([trade, _mt5_trade("2024-01-03", 50.0)])

    assert _reference_sharpe_ratio(reference) is None


def test_mt5_row_reports_printed_sharpe_separately_from_the_comparison(matrix):
    """The matrix keeps MT5's printed ratio for audit but compares the
    same-formula recomputation."""
    row = {item["case_id"]: item for item in matrix["rows"]}[
        "intraday_volatility_breakout_sp500_m1"
    ]

    reference = MT5XLSXImporter().load(
        Path(row["reference_path"]),
        broker_clock=row["reference_broker_clock"],
    )

    assert row["reference"]["sharpe_ratio_reported"] is not None
    assert row["reference"]["sharpe_ratio"] is not None
    # The comparison value IS the canonical recomputation on the imported
    # trade list — not the printed MT5 number, and not a lookalike.
    assert row["reference"]["sharpe_ratio"] == pytest.approx(
        _reference_sharpe_ratio(reference), rel=1e-6
    )
    assert row["reference"]["sharpe_ratio"] != pytest.approx(
        row["reference"]["sharpe_ratio_reported"], rel=1e-3
    )
    assert any("sharpe_ratio_reported" in note for note in row["notes"])


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


def test_reference_sharpe_fails_closed_when_exits_share_one_day():
    """#349 review: exits all landing on the start day leave fewer than two
    daily returns, so the rebuilt curve's Sharpe is undefined. It must
    surface as None — not the fabricated 0.0 compute_stats used to publish
    for a metric named in `undefined_metrics`."""
    trades = [
        _mt5_trade("2024-01-02", 100.0),
        _mt5_trade("2024-01-02", -50.0),
    ]
    reference = _mt5_like_reference(trades)

    assert _reference_sharpe_ratio(reference) is None


def _expected_parity_sharpe(trades, pnl_by_day):
    """Rebuild the calendar-filled curve _reference_sharpe_ratio uses and
    return compute_stats' Sharpe — the same-formula expectation."""
    from datetime import datetime, timedelta, timezone

    from runner.stats import compute_stats
    from shared.models import EquityPoint

    start_candidates = []
    for trade in trades:
        entry_ts = pd.Timestamp(trade.entry_time)
        start_candidates.append(entry_ts)
    start = min(start_candidates)
    last_day = max(
        pd.Timestamp(t.exits[-1].time).date() for t in trades
    )
    equity = 100_000.0
    points = [EquityPoint(time=start.isoformat(), equity=equity)]
    day = start.date()
    while day <= last_day:
        equity += pnl_by_day.get(day.isoformat(), 0.0)
        stamp = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        points.append(EquityPoint(time=stamp.isoformat(), equity=equity))
        day += timedelta(days=1)
    stats = compute_stats([], initial_capital=100_000.0, equity_curve=points)
    return stats["sharpe_ratio"]


def test_reference_sharpe_books_net_pnl_fallback_as_pnl_minus_fees():
    """#349 mutation survivor: with net_pnl=None the curve must book
    pnl - fees. A sign flip to pnl + fees moves every day's equity and
    changes the Sharpe."""
    trades = []
    for day in ["02", "03", "04", "05", "08", "09", "10", "11"]:
        trade = _mt5_trade(f"2024-01-{day}", 100.0)
        trade.net_pnl = None
        trade.fees = 30.0
        trades.append(trade)
    reference = _mt5_like_reference(trades)

    sharpe = _reference_sharpe_ratio(reference)

    pnl_by_day = {
        f"2024-01-{day}": 70.0
        for day in ["02", "03", "04", "05", "08", "09", "10", "11"]
    }
    expected = _expected_parity_sharpe(trades, pnl_by_day)
    assert sharpe == pytest.approx(expected, abs=1e-4)


def test_reference_sharpe_books_pnl_at_the_last_exit_for_partial_exits():
    """#349 mutation survivor: a position with several exits books its whole
    net_pnl on the LAST exit's day, not the first — booking early shifts the
    equity path and the Sharpe."""
    trade = Trade(
        id=1,
        symbol="EURUSD",
        direction="LONG",
        entry_time="2024-01-02T08:00:00Z",
        entry_price=1.1,
        quantity=2.0,
        exits=[
            TradeExit(time="2024-01-03T16:00:00Z", price=1.1, quantity=1.0, pnl=50.0),
            TradeExit(time="2024-01-08T16:00:00Z", price=1.1, quantity=1.0, pnl=50.0),
        ],
        pnl=100.0,
        net_pnl=100.0,
    )
    other = _mt5_trade("2024-01-05", -40.0)
    trades = [trade, other]
    reference = _mt5_like_reference(trades)

    sharpe = _reference_sharpe_ratio(reference)

    # Correct booking: +100 on Jan 8 (last exit), -40 on Jan 5.
    pnl_by_day = {"2024-01-08": 100.0, "2024-01-05": -40.0}
    expected = _expected_parity_sharpe(trades, pnl_by_day)
    assert sharpe == pytest.approx(expected, abs=1e-4)
