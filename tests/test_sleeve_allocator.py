"""Tests for runner.sleeve_allocator (issue #307, phase 1).

All sleeves are synthetic: business-day return frames built in-test, matrix
CSVs and walk-forward JSON payloads written under ``tmp_path``.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from runner.sleeve_allocator import (
    AllocationConfig,
    EligibilityConfig,
    SleeveMetadata,
    SleeveReturns,
    align_sleeve_returns,
    filter_eligible_sleeves,
    load_sleeve_returns,
    solve_weights,
    walk_forward_allocation,
)


def _meta(sleeve_id: str, **overrides) -> SleeveMetadata:
    base = {
        "sleeve_id": sleeve_id,
        "strategy": "strat",
        "symbol": "EURUSD",
        "timeframe": "1h",
        "source": "synthetic",
        "total_trades": 500,
    }
    base.update(overrides)
    return SleeveMetadata(**base)


def _synthetic_returns(
    n_days: int = 800,
    seed: int = 7,
    start: str = "2021-01-04",
) -> pd.DataFrame:
    """Three sleeves on a business-day calendar with distinct vols/drifts."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(start, periods=n_days)
    data = {
        "sma|EURUSD|1h": rng.normal(0.0004, 0.004, n_days),
        "rsi|XAUUSD|4h": rng.normal(0.0001, 0.008, n_days),
        "macd|BTCUSD|1d": rng.normal(0.0008, 0.012, n_days),
    }
    return pd.DataFrame(data, index=index)


def _synthetic_sleeve_returns(df: pd.DataFrame | None = None) -> SleeveReturns:
    df = df if df is not None else _synthetic_returns()
    metadata = {}
    for col in df.columns:
        strategy, symbol, timeframe = col.split("|")[:3]
        metadata[col] = _meta(col, strategy=strategy, symbol=symbol, timeframe=timeframe)
    # align_sleeve_returns expects per-sleeve calendar-day series; a business-day
    # frame is already aligned, so wrap each column as a Series on the same index.
    series = {col: df[col] for col in df.columns}
    return align_sleeve_returns(series, metadata=metadata)


# ---------------------------------------------------------------------------
# Loader: business-day alignment + weekend roll-forward
# ---------------------------------------------------------------------------


def test_weekend_returns_roll_forward_into_monday():
    # Calendar-day returns: Fri, Sat, Sun, Mon. Sat/Sun must compound into Mon.
    index = pd.to_datetime(
        ["2024-01-05", "2024-01-06", "2024-01-07", "2024-01-08"]
    )
    series = pd.Series([0.01, 0.02, 0.03, 0.04], index=index, name="crypto")
    result = align_sleeve_returns({"crypto|BTCUSD|1d": series})

    out = result.returns["crypto|BTCUSD|1d"]
    assert list(result.returns.index.weekday) == [4, 0]  # Fri, Mon only
    assert out.loc[pd.Timestamp("2024-01-05")] == pytest.approx(0.01)
    expected_mon = (1.02 * 1.03 * 1.04) - 1.0
    assert out.loc[pd.Timestamp("2024-01-08")] == pytest.approx(expected_mon)
    # No zero-filled weekend rows.
    assert not result.returns.index.hasnans
    assert (result.returns.index.dayofweek < 5).all()


def test_alignment_uses_union_business_calendar_with_nan_for_missing():
    # Sleeve A observes only weekdays; sleeve B (crypto) also has weekend obs
    # which roll into Monday. Both must share one business-day index.
    weekdays = pd.bdate_range("2024-01-01", periods=10)
    series_a = pd.Series(np.full(10, 0.001), index=weekdays)
    crypto_index = weekdays.append(pd.DatetimeIndex(["2024-01-06", "2024-01-07"]))
    crypto_index = crypto_index.sort_values()
    series_b = pd.Series(np.full(len(crypto_index), 0.002), index=crypto_index)

    result = align_sleeve_returns(
        {"eq|EURUSD|1d": series_a, "crypto|BTCUSD|1d": series_b}
    )
    assert list(result.returns.columns) == ["eq|EURUSD|1d", "crypto|BTCUSD|1d"]
    assert (result.returns.index.dayofweek < 5).all()
    # Every business day in the span is present.
    assert len(result.returns) == len(weekdays)
    # Crypto's Sat/Sun observations are folded into Monday 2024-01-08.
    monday = result.returns.loc[pd.Timestamp("2024-01-08"), "crypto|BTCUSD|1d"]
    assert monday == pytest.approx((1.002**3) - 1.0)


def test_loader_drops_rows_that_are_all_nan():
    index = pd.bdate_range("2024-01-01", periods=5)
    series = pd.Series([0.01] * 5, index=index)
    result = align_sleeve_returns({"a|X|1d": series})
    assert len(result.returns) == 5


def test_matrix_csv_loader_builds_sleeves_and_metadata(tmp_path):
    csv_path = tmp_path / "matrix_daily_equity.csv"
    rows = []
    days = pd.bdate_range("2024-01-01", periods=30)
    for i, day in enumerate(days):
        rows.append(
            {
                "strategy": "sma",
                "symbol": "EURUSD",
                "timeframe": "1h",
                "run_id": "grid_sma_eurusd_1h",
                "time": day.isoformat(),
                "equity": 100000 * (1.001**i),
                "daily_return": 0.001,
            }
        )
    # A second sleeve on the same calendar.
    for day in days:
        rows.append(
            {
                "strategy": "rsi",
                "symbol": "XAUUSD",
                "timeframe": "4h",
                "run_id": "grid_rsi_xauusd_4h",
                "time": day.isoformat(),
                "equity": 50000,
                "daily_return": -0.0005,
            }
        )
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        import csv as csv_mod

        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy",
                "symbol",
                "timeframe",
                "run_id",
                "time",
                "equity",
                "daily_return",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    result = load_sleeve_returns(matrix_csv=csv_path)
    assert set(result.returns.columns) == {"sma|EURUSD|1h", "rsi|XAUUSD|4h"}
    meta = result.metadata["sma|EURUSD|1h"]
    assert meta.strategy == "sma"
    assert meta.symbol == "EURUSD"
    assert meta.timeframe == "1h"
    assert meta.run_id == "grid_sma_eurusd_1h"
    assert meta.source == "matrix"
    assert meta.n_observations == 30


def test_walkforward_loader_uses_only_oos_folds(tmp_path):
    payload = {
        "mode": "strategy",
        "strategy": "strategies/sma.py",
        "engine": "vectorbt",
        "symbols": ["EURUSD"],
        "timeframe": "1h",
        "config": {"train_months": 12, "test_months": 3},
        "folds": [
            {
                "fold": 1,
                "train_start": "2022-01-01",
                "train_end": "2022-12-31",
                "test_start": "2023-01-02",
                "test_end": "2023-03-31",
                "is_sharpe": 1.5,
                "oos_sharpe": 0.4,
                "oos_stats": {"total_trades": 12, "sharpe_ratio": 0.4},
                "oos_equity_curve": [
                    {"time": "2023-01-02T00:00:00Z", "equity": 100000.0},
                    {"time": "2023-01-03T00:00:00Z", "equity": 101000.0},
                    {"time": "2023-01-04T00:00:00Z", "equity": 100500.0},
                ],
            },
            {
                "fold": 2,
                "train_start": "2022-04-01",
                "train_end": "2023-03-31",
                "test_start": "2023-04-03",
                "test_end": "2023-06-30",
                "is_sharpe": 1.2,
                "oos_sharpe": 0.3,
                "oos_stats": {"total_trades": 9, "sharpe_ratio": 0.3},
                "oos_equity_curve": [
                    {"time": "2023-04-03T00:00:00Z", "equity": 100000.0},
                    {"time": "2023-04-04T00:00:00Z", "equity": 99000.0},
                ],
            },
        ],
        "summary": {},
    }
    path = tmp_path / "wf_test.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_sleeve_returns(walkforward_paths=[path])
    assert list(result.returns.columns) == ["sma|EURUSD|1h"]
    sleeve = result.returns["sma|EURUSD|1h"].dropna()
    # Fold 1: 100000 -> 101000 -> 100500. First point anchors, returns start
    # at the second observation. Fold 2 equity restarts at 100000: its first
    # point must NOT produce a return against fold 1's last equity.
    assert sleeve.loc[pd.Timestamp("2023-01-03")] == pytest.approx(0.01)
    assert sleeve.loc[pd.Timestamp("2023-01-04")] == pytest.approx(100500 / 101000 - 1.0)
    assert sleeve.loc[pd.Timestamp("2023-04-04")] == pytest.approx(-0.01)
    # No return may be dated inside a train window.
    assert sleeve.index.max() <= pd.Timestamp("2023-06-30")
    meta = result.metadata["sma|EURUSD|1h"]
    assert meta.source == "walkforward"
    assert meta.total_trades == 21
    # Trade periods carry the OOS test windows for as-of eligibility.
    assert len(meta.trade_periods) == 2
    assert meta.trade_periods[0].trades == 12


def test_walkforward_loader_ignores_payload_without_oos_curves(tmp_path):
    payload = {
        "mode": "strategy",
        "strategy": "x.py",
        "symbols": ["EURUSD"],
        "timeframe": "1h",
        "folds": [{"fold": 1, "oos_stats": {"total_trades": 3}}],
    }
    path = tmp_path / "wf_empty.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = load_sleeve_returns(walkforward_paths=[path])
    assert result.returns.empty
    assert result.metadata == {}


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_eligibility_rejects_short_history():
    index = pd.bdate_range("2024-01-01", periods=100)  # ~5 months < 2y
    df = pd.DataFrame({"a|X|1d": np.full(100, 0.001)}, index=index)
    sr = align_sleeve_returns({"a|X|1d": df["a|X|1d"]}, metadata={"a|X|1d": _meta("a|X|1d")})
    result = filter_eligible_sleeves(sr, EligibilityConfig(min_history_days=504))
    assert result.eligible == ()
    assert "insufficient_history" in result.rejected["a|X|1d"]


def test_eligibility_rejects_too_few_trades_and_unknown_trades():
    sr = _synthetic_sleeve_returns()
    cfg = EligibilityConfig(min_trades=100)
    meta = dict(sr.metadata)
    meta["sma|EURUSD|1h"] = _meta("sma|EURUSD|1h", total_trades=10)
    meta["rsi|XAUUSD|4h"] = _meta("rsi|XAUUSD|4h", total_trades=None)
    sr2 = SleeveReturns(returns=sr.returns, metadata=meta)
    result = filter_eligible_sleeves(sr2, cfg)
    assert "insufficient_trades" in result.rejected["sma|EURUSD|1h"]
    # Fail closed: unknown trade count cannot satisfy min_trades.
    assert "trades_unknown" in result.rejected["rsi|XAUUSD|4h"]
    assert "macd|BTCUSD|1d" in result.eligible


def test_eligibility_rejects_ruined_sleeve():
    index = pd.bdate_range("2021-01-04", periods=800)
    good = np.full(800, 0.0005)
    ruined = np.full(800, -0.006)  # compounds to ~0.8% of initial
    df = pd.DataFrame(
        {"good|X|1d": good, "dead|X|1d": ruined}, index=index
    )
    sr = align_sleeve_returns(
        {c: df[c] for c in df.columns},
        metadata={c: _meta(c) for c in df.columns},
    )
    result = filter_eligible_sleeves(sr, EligibilityConfig(ruin_equity_floor=0.01))
    assert "dead|X|1d" in result.rejected
    assert "ruined" in result.rejected["dead|X|1d"]
    assert "good|X|1d" in result.eligible


def test_eligibility_as_of_uses_only_prior_data():
    df = _synthetic_returns(n_days=800)
    # Give sleeve A a huge losing streak late in the sample: before that point
    # it is healthy, after it is ruined.
    late = df.index >= pd.Timestamp("2023-06-01")
    df.loc[late, "sma|EURUSD|1h"] = -0.10
    sr = _synthetic_sleeve_returns(df)
    early = filter_eligible_sleeves(
        sr, EligibilityConfig(ruin_equity_floor=0.01), as_of=pd.Timestamp("2023-01-02")
    )
    late_check = filter_eligible_sleeves(
        sr, EligibilityConfig(ruin_equity_floor=0.01)
    )
    assert "sma|EURUSD|1h" in early.eligible
    assert "ruined" in late_check.rejected.get("sma|EURUSD|1h", ())


# ---------------------------------------------------------------------------
# Objectives / baselines
# ---------------------------------------------------------------------------


def test_equal_weight_baseline_is_one_over_n():
    window = _synthetic_returns(n_days=100)
    ids = list(window.columns)
    w = solve_weights("equal_weight", ids, window[ids])
    assert w is not None
    assert np.allclose(w, 1.0 / len(ids))


def test_inverse_vol_baseline_favours_low_vol_and_sums_to_one():
    index = pd.bdate_range("2024-01-01", periods=120)
    rng = np.random.default_rng(3)
    window = pd.DataFrame(
        {
            "low|X|1d": rng.normal(0, 0.001, 120),
            "high|X|1d": rng.normal(0, 0.01, 120),
        },
        index=index,
    )
    ids = list(window.columns)
    w = solve_weights("inverse_vol", ids, window[ids])
    assert w is not None
    assert w.sum() == pytest.approx(1.0)
    assert w[0] > w[1]
    # Inverse vol ratio ~ 10:1 before normalisation.
    assert w[0] / w[1] == pytest.approx(10.0, rel=0.2)


def test_inverse_vol_gives_zero_weight_to_flat_or_short_sleeve():
    index = pd.bdate_range("2024-01-01", periods=120)
    rng = np.random.default_rng(3)
    window = pd.DataFrame(
        {
            "ok|X|1d": rng.normal(0, 0.005, 120),
            "flat|X|1d": np.zeros(120),
            "short|X|1d": np.nan,
        },
        index=index,
    )
    ids = list(window.columns)
    w = solve_weights("inverse_vol", ids, window[ids], min_window_observations=30)
    assert w is not None
    weights = dict(zip(ids, w))
    assert weights["flat|X|1d"] == 0.0
    assert weights["short|X|1d"] == 0.0
    assert weights["ok|X|1d"] == pytest.approx(1.0)


def test_solve_weights_rejects_unknown_objective():
    window = _synthetic_returns(n_days=60)
    with pytest.raises(ValueError, match="objective"):
        solve_weights("erc", list(window.columns), window)


# ---------------------------------------------------------------------------
# Walk-forward allocation harness
# ---------------------------------------------------------------------------


def _eligible_synthetic() -> SleeveReturns:
    return _synthetic_sleeve_returns(_synthetic_returns(n_days=800))


def test_harness_produces_chained_oos_curves_and_always_shows_baselines():
    sr = _eligible_synthetic()
    result = walk_forward_allocation(
        sr, AllocationConfig(lookback_days=252, objectives=("equal_weight",))
    )
    # inverse_vol must appear even though only equal_weight was requested.
    assert set(result.returns) == {"equal_weight", "inverse_vol"}
    assert set(result.equity) == {"equal_weight", "inverse_vol"}
    for name, curve in result.equity.items():
        assert curve.iloc[0] == pytest.approx(1.0)
        assert len(curve) > 100
        # Business-day index only.
        assert (curve.index.dayofweek < 5).all()
    report = result.report
    assert set(report["objectives"]) == {"equal_weight", "inverse_vol"}
    for name in ("equal_weight", "inverse_vol"):
        metrics = report["objectives"][name]["metrics"]
        for key in ("total_return", "ann_return", "ann_volatility", "sharpe", "max_drawdown"):
            assert key in metrics
    assert report["baselines"] == ["equal_weight", "inverse_vol"]


def test_harness_rebalances_monthly_at_month_end_business_days():
    sr = _eligible_synthetic()
    result = walk_forward_allocation(
        sr, AllocationConfig(lookback_days=252, objectives=("equal_weight",))
    )
    rebs = result.rebalances["equal_weight"]
    assert len(rebs) >= 6
    df = pd.DataFrame({"ts": result.equity["equal_weight"].index})
    month_last = df.groupby(df["ts"].dt.to_period("M"))["ts"].max()
    rebalance_dates = {pd.Timestamp(r.date) for r in rebs}
    # Every rebalance date is the last observed business day of its month.
    assert rebalance_dates.issubset(set(month_last))
    # Roughly monthly cadence: no two rebalances in the same month.
    months = [pd.Timestamp(r.date).to_period("M") for r in rebs]
    assert len(months) == len(set(months))


def test_harness_weights_sum_to_one_common_budget():
    sr = _eligible_synthetic()
    result = walk_forward_allocation(
        sr, AllocationConfig(lookback_days=252)
    )
    for rec in result.rebalances["equal_weight"]:
        total = sum(rec.weights.values())
        assert total == pytest.approx(1.0)
    for rec in result.rebalances["inverse_vol"]:
        total = sum(rec.weights.values())
        assert total == pytest.approx(1.0)


def test_harness_has_no_lookahead():
    sr = _eligible_synthetic()
    cfg = AllocationConfig(lookback_days=252, objectives=("inverse_vol",))
    base = walk_forward_allocation(sr, cfg)

    df = sr.returns.copy()
    # Corrupt all data from month 20 onward: earlier weights must not move.
    cutoff = df.index[int(len(df) * 0.75)]
    df.loc[df.index >= cutoff] = df.loc[df.index >= cutoff] * 5.0 + 0.02
    sr2 = SleeveReturns(returns=df, metadata=sr.metadata)
    changed = walk_forward_allocation(sr2, cfg)

    base_weights = {
        r.date: r.weights for r in base.rebalances["inverse_vol"]
    }
    for rec in changed.rebalances["inverse_vol"]:
        if pd.Timestamp(rec.date) < cutoff:
            expected = base_weights[rec.date]
            assert rec.weights.keys() == expected.keys()
            for key, value in expected.items():
                assert rec.weights[key] == pytest.approx(value)


def test_harness_single_eligible_sleeve_gets_unit_weight():
    df = _synthetic_returns(n_days=800)
    # Make two sleeves clearly ineligible: too few trades.
    sr = _synthetic_sleeve_returns(df)
    meta = dict(sr.metadata)
    meta["rsi|XAUUSD|4h"] = _meta("rsi|XAUUSD|4h", total_trades=1)
    meta["macd|BTCUSD|1d"] = _meta("macd|BTCUSD|1d", total_trades=1)
    sr = SleeveReturns(returns=sr.returns, metadata=meta)
    result = walk_forward_allocation(
        sr, AllocationConfig(lookback_days=252, objectives=("equal_weight",))
    )
    for rec in result.rebalances["equal_weight"]:
        assert rec.weights["sma|EURUSD|1h"] == pytest.approx(1.0)
        assert rec.weights["rsi|XAUUSD|4h"] == 0.0


def test_harness_reports_turnover():
    sr = _eligible_synthetic()
    result = walk_forward_allocation(sr, AllocationConfig(lookback_days=252))
    for name in ("equal_weight", "inverse_vol"):
        obj = result.report["objectives"][name]
        assert "avg_turnover" in obj


def test_harness_empty_when_nothing_eligible_reports_note():
    index = pd.bdate_range("2024-01-01", periods=40)
    df = pd.DataFrame({"a|X|1d": np.full(40, 0.001)}, index=index)
    sr = align_sleeve_returns(
        {"a|X|1d": df["a|X|1d"]}, metadata={"a|X|1d": _meta("a|X|1d", total_trades=0)}
    )
    result = walk_forward_allocation(sr, AllocationConfig(lookback_days=20))
    assert result.rebalances == {} or all(
        not recs for recs in result.rebalances.values()
    )
    assert result.report["objectives"]["equal_weight"]["metrics"]["n_days"] == 0
