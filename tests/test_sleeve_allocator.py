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
    TradePeriod,
    align_sleeve_returns,
    filter_eligible_sleeves,
    load_sleeve_returns,
    solve_weights,
    walk_forward_allocation,
)
from runner.sleeve_allocator.harness import _weights_for_objective
from runner.sleeve_allocator.loader import _oos_fold_returns


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


def _monthly_trade_periods(index: pd.DatetimeIndex, trades: int = 15) -> tuple:
    """One TradePeriod per calendar month, ending on the month's last day.

    The harness evaluates eligibility at each month-end rebalance ``t`` with
    ``end < t``, so a period ending on ``t`` itself is (correctly) excluded —
    the count at ``t`` covers every PRIOR month.
    """
    periods = []
    for month in pd.DatetimeIndex(index).to_period("M").unique():
        days = pd.DatetimeIndex(index)[
            pd.DatetimeIndex(index).to_period("M") == month
        ]
        periods.append(
            TradePeriod(
                start=days[0].date().isoformat(),
                end=days[-1].date().isoformat(),
                trades=trades,
            )
        )
    return tuple(periods)


def _synthetic_sleeve_returns(df: pd.DataFrame | None = None) -> SleeveReturns:
    df = df if df is not None else _synthetic_returns()
    periods = _monthly_trade_periods(df.index)
    metadata = {}
    for col in df.columns:
        strategy, symbol, timeframe = col.split("|")[:3]
        metadata[col] = _meta(
            col,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            trade_periods=periods,
        )
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


def test_loader_unobserved_sleeve_days_stay_nan():
    # A business day one sleeve did not observe stays NaN on the shared
    # calendar — missing data, never zero-filled.
    index = pd.bdate_range("2024-01-01", periods=5)
    sparse = pd.Series([0.01, np.nan, 0.02, np.nan, 0.03], index=index)
    full = pd.Series([0.005] * 5, index=index)
    result = align_sleeve_returns({"a|X|1d": sparse, "b|Y|1d": full})
    out = result.returns["a|X|1d"]
    assert len(result.returns) == 5
    assert np.isnan(out.iloc[1]) and np.isnan(out.iloc[3])
    assert out.iloc[0] == pytest.approx(0.01)


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
        sr,
        EligibilityConfig(min_history_days=0, ruin_equity_floor=0.01),
        as_of=pd.Timestamp("2023-01-02"),
    )
    late_check = filter_eligible_sleeves(
        sr, EligibilityConfig(min_history_days=0, ruin_equity_floor=0.01)
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
        if rec.cash:
            # Nothing eligible: the whole budget sits in cash.
            assert total == pytest.approx(0.0)
        else:
            assert total == pytest.approx(1.0)
    for rec in result.rebalances["inverse_vol"]:
        total = sum(rec.weights.values())
        assert total == pytest.approx(0.0 if rec.cash else 1.0)


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
    non_cash = [r for r in result.rebalances["equal_weight"] if not r.cash]
    assert non_cash, "expected at least one non-cash rebalance"
    for rec in non_cash:
        assert rec.weights["sma|EURUSD|1h"] == pytest.approx(1.0)
        assert rec.weights["rsi|XAUUSD|4h"] == 0.0


def test_harness_reports_turnover():
    sr = _eligible_synthetic()
    result = walk_forward_allocation(sr, AllocationConfig(lookback_days=252))
    for name in ("equal_weight", "inverse_vol"):
        obj = result.report["objectives"][name]
        assert "avg_turnover" in obj


def test_harness_empty_when_nothing_eligible_goes_to_cash():
    """No eligible sleeve -> the whole budget sits in cash: all-zero weights,
    flat (0.0) returns, and the record is flagged ``cash`` — the previous
    allocation is never carried onto rejected sleeves."""
    index = pd.bdate_range("2024-01-01", periods=40)
    df = pd.DataFrame({"a|X|1d": np.full(40, 0.001)}, index=index)
    sr = align_sleeve_returns(
        {"a|X|1d": df["a|X|1d"]}, metadata={"a|X|1d": _meta("a|X|1d", total_trades=0)}
    )
    result = walk_forward_allocation(sr, AllocationConfig(lookback_days=20))
    records = result.rebalances["equal_weight"]
    assert records, "cash rebalances must still be recorded"
    for rec in records:
        assert rec.cash is True
        assert rec.n_eligible == 0
        assert all(w == 0.0 for w in rec.weights.values())
    returns = result.returns["equal_weight"]
    assert len(returns) > 0
    assert (returns == 0.0).all()
    assert result.report["objectives"]["equal_weight"]["cash_rebalances"] == len(records)
    assert result.report["objectives"]["equal_weight"]["metrics"]["total_return"] == 0.0


# ---------------------------------------------------------------------------
# Point-in-time / no-lookahead pins (bundle 27 mutants N1, N2, N3, N9-N12)
# ---------------------------------------------------------------------------


def test_weights_at_rebalance_ignore_the_rebalance_day_itself():
    """N1: the estimation window is STRICTLY before the rebalance day.

    Corrupt ONLY the return observed on rebalance day ``d``: weights decided
    at ``d`` must be identical to the uncorrupted run. A ``<=`` boundary
    would let ``d``'s own return into the estimation window.
    """
    sr = _eligible_synthetic()
    cfg = AllocationConfig(lookback_days=60, objectives=("inverse_vol",))
    base = walk_forward_allocation(sr, cfg)
    records = base.rebalances["inverse_vol"]
    assert len(records) >= 3
    target = records[-1]
    assert target.n_eligible > 0, "need a non-cash rebalance to pin weights"
    day = pd.Timestamp(target.date)

    df = sr.returns.copy()
    df.loc[day, :] = df.loc[day, :] * 100.0 + 0.5
    sr2 = SleeveReturns(returns=df, metadata=sr.metadata)
    changed = walk_forward_allocation(sr2, cfg)
    changed_at_d = {r.date: r.weights for r in changed.rebalances["inverse_vol"]}
    assert changed_at_d[day.date().isoformat()] == pytest.approx(
        target.weights, abs=1e-12
    )


def test_eligibility_as_of_excludes_observations_on_as_of_day():
    """N2: ``_slice_before`` is exclusive — a ruin print ON ``as_of`` is not
    evidence the decision could have seen."""
    index = pd.bdate_range("2021-01-04", periods=800)
    returns = np.full(800, 0.0005)
    as_of_day = index[400]
    returns[400] = -0.995  # single-day wipeout -> running equity ~0.6% -> ruined
    df = pd.DataFrame({"a|X|1d": returns}, index=index)
    sr = align_sleeve_returns({"a|X|1d": df["a|X|1d"]}, metadata={"a|X|1d": _meta("a|X|1d")})
    cfg = EligibilityConfig(min_history_days=0, min_trades=0, ruin_equity_floor=0.01)

    before = filter_eligible_sleeves(sr, cfg, as_of=as_of_day)
    assert "a|X|1d" in before.eligible
    after = filter_eligible_sleeves(
        sr, cfg, as_of=as_of_day + pd.Timedelta(days=1)
    )
    assert "ruined" in after.rejected["a|X|1d"]


def test_trade_period_ending_on_as_of_day_does_not_count():
    """Blocker 3: ``end <= as_of`` was an off-by-one vs the exclusive
    observation cutoff — a period ending ON the rebalance day holds trades
    the decision cannot have seen."""
    index = pd.bdate_range("2021-01-04", periods=800)
    df = pd.DataFrame({"a|X|1d": np.full(800, 0.0005)}, index=index)
    periods = (TradePeriod(start="2020-01-01", end="2023-06-30", trades=200),)
    sr = align_sleeve_returns(
        {"a|X|1d": df["a|X|1d"]},
        metadata={"a|X|1d": _meta("a|X|1d", trade_periods=periods)},
    )
    cfg = EligibilityConfig(min_history_days=0, min_trades=100)
    # as_of one day after the period end: counted.
    ok = filter_eligible_sleeves(sr, cfg, as_of=pd.Timestamp("2023-07-03"))
    assert "a|X|1d" in ok.eligible
    # as_of ON the period end: NOT counted (period contains unseen trades).
    edge = filter_eligible_sleeves(sr, cfg, as_of=pd.Timestamp("2023-06-30"))
    assert "insufficient_trades" in edge.rejected["a|X|1d"]


def test_full_sample_trade_counts_rejected_under_as_of_unless_opted_in():
    """Should-fix 6 (conservative reading of maintainer Q5): a
    ``total_trades``-only count is future-inclusive, so point-in-time
    eligibility rejects it unless explicitly opted in."""
    index = pd.bdate_range("2021-01-04", periods=800)
    df = pd.DataFrame({"a|X|1d": np.full(800, 0.0005)}, index=index)
    sr = align_sleeve_returns({"a|X|1d": df["a|X|1d"]}, metadata={"a|X|1d": _meta("a|X|1d")})
    cfg = EligibilityConfig(min_history_days=0, min_trades=100)
    as_of = pd.Timestamp("2023-06-30")

    strict = filter_eligible_sleeves(sr, cfg, as_of=as_of)
    assert "trades_unknown_as_of" in strict.rejected["a|X|1d"]

    opted = EligibilityConfig(
        min_history_days=0, min_trades=100, allow_full_sample_trade_counts=True
    )
    loose = filter_eligible_sleeves(sr, opted, as_of=as_of)
    assert "a|X|1d" in loose.eligible
    assert loose.trade_count_basis["a|X|1d"] == "full_sample"

    # Without as_of the full-sample count is legitimate evidence.
    full = filter_eligible_sleeves(sr, cfg)
    assert "a|X|1d" in full.eligible


def test_weights_for_objective_empty_eligible_is_cash_not_carry():
    """N5: nothing eligible -> all-zero weights flagged cash, even when a
    previous allocation exists (it may sit on just-rejected sleeves)."""
    cfg = AllocationConfig()
    columns = ["a|X|1d", "b|X|1d"]
    window = pd.DataFrame(0.0, index=pd.bdate_range("2024-01-01", periods=40), columns=columns)
    weights, carried, fallback, cash = _weights_for_objective(
        "inverse_vol", [], window, {"a|X|1d": 0.7, "b|X|1d": 0.3}, columns, cfg
    )
    assert weights == {"a|X|1d": 0.0, "b|X|1d": 0.0}
    assert carried is False and fallback is False and cash is True


def test_weights_for_objective_solver_failure_carries_only_eligible_renormalized():
    """Solver failure: previous weights survive ONLY on still-eligible
    sleeves, renormalised; a just-rejected sleeve keeps nothing."""
    cfg = AllocationConfig()
    columns = ["a|X|1d", "b|X|1d", "c|X|1d"]
    # Flat window -> inverse_vol cannot estimate (every vol is 0).
    window = pd.DataFrame(0.0, index=pd.bdate_range("2024-01-01", periods=40), columns=columns)
    weights, carried, fallback, cash = _weights_for_objective(
        "inverse_vol",
        ["a|X|1d", "c|X|1d"],
        window,
        {"a|X|1d": 0.2, "b|X|1d": 0.5, "c|X|1d": 0.3},
        columns,
        cfg,
    )
    assert carried is True and fallback is False and cash is False
    # b was rejected: its 0.5 is gone; a:c renormalise 0.2:0.3 -> 0.4:0.6.
    assert weights["a|X|1d"] == pytest.approx(0.4)
    assert weights["b|X|1d"] == 0.0
    assert weights["c|X|1d"] == pytest.approx(0.6)


def test_weights_for_objective_solver_failure_falls_back_to_equal_weight():
    """Nothing eligible to carry -> 1/N over the eligible set, flagged
    ``fallback_equal_weight`` (NOT carried)."""
    cfg = AllocationConfig()
    columns = ["a|X|1d", "b|X|1d"]
    window = pd.DataFrame(0.0, index=pd.bdate_range("2024-01-01", periods=40), columns=columns)
    # First rebalance: no previous weights at all.
    weights, carried, fallback, cash = _weights_for_objective(
        "inverse_vol", ["a|X|1d", "b|X|1d"], window, None, columns, cfg
    )
    assert weights["a|X|1d"] == pytest.approx(0.5)
    assert weights["b|X|1d"] == pytest.approx(0.5)
    assert carried is False and fallback is True and cash is False
    # Previous weights all sat on now-ineligible sleeves -> same fallback.
    weights2, carried2, fallback2, _ = _weights_for_objective(
        "inverse_vol",
        ["a|X|1d", "b|X|1d"],
        window,
        {"a|X|1d": 0.0, "b|X|1d": 0.0, "gone|X|1d": 1.0},
        columns + ["gone|X|1d"],
        cfg,
    )
    assert weights2["a|X|1d"] == pytest.approx(0.5)
    assert carried2 is False and fallback2 is True


def test_chained_returns_match_hand_computation_with_nan_day():
    """N11/N12 pin: exact chained daily returns for a two-sleeve segment,
    including a NaN sleeve-day (flat contribution) — a ``fillna`` change or
    a dropped lookback window breaks these numbers."""
    index = pd.bdate_range("2024-01-01", periods=80)
    rng = np.random.default_rng(11)
    a = rng.normal(0.001, 0.01, 80)
    b = rng.normal(-0.0005, 0.02, 80)
    b[37] = np.nan  # sleeve B unobserved: contributes 0 that day
    df = pd.DataFrame({"a|X|1d": a, "b|X|1d": b}, index=index)
    sr = align_sleeve_returns(
        {c: df[c] for c in df.columns},
        metadata={c: _meta(c) for c in df.columns},
    )
    cfg = AllocationConfig(
        lookback_days=20,
        min_window_observations=10,
        objectives=("equal_weight",),
        eligibility=EligibilityConfig(min_history_days=0, min_trades=0),
    )
    result = walk_forward_allocation(sr, cfg)
    chained = result.returns["equal_weight"]
    assert len(chained) > 0

    # Recompute independently: every applied day is 0.5*a + 0.5*b, NaN -> 0.
    frame_index = sr.returns.index
    expected = {}
    rebs = [pd.Timestamp(r.date) for r in result.rebalances["equal_weight"]]
    for i, reb in enumerate(rebs):
        end = rebs[i + 1] if i + 1 < len(rebs) else None
        seg_idx = frame_index[
            (frame_index >= reb) & (frame_index < end if end is not None else True)
        ]
        for day in seg_idx:
            ra = float(df.loc[day, "a|X|1d"])
            rb = df.loc[day, "b|X|1d"]
            rb = 0.0 if pd.isna(rb) else float(rb)
            expected[day] = 0.5 * ra + 0.5 * rb
    expected_series = pd.Series(expected).sort_index()
    pd.testing.assert_series_equal(
        chained.sort_index(),
        expected_series,
        check_names=False,
        check_freq=False,
        atol=1e-12,
    )


def test_estimation_window_is_bounded_to_lookback_days():
    """N12: ``tail(lookback_days)`` — an expanding window would let ancient
    high-vol data dilute the recent estimate."""
    index = pd.bdate_range("2021-01-04", periods=400)
    rng = np.random.default_rng(5)
    early = rng.normal(0.0, 0.05, 340)   # very volatile early regime
    late = rng.normal(0.001, 0.002, 60)  # calm recent regime
    df = pd.DataFrame(
        {"a|X|1d": np.concatenate([early, late]), "b|X|1d": rng.normal(0, 0.01, 400)},
        index=index,
    )
    sr = _synthetic_sleeve_returns(
        df.rename(columns={"a|X|1d": "sma|EURUSD|1h", "b|X|1d": "rsi|XAUUSD|4h"})
    )
    cfg = AllocationConfig(
        lookback_days=60,
        objectives=("inverse_vol",),
        eligibility=EligibilityConfig(min_history_days=0),
    )
    result = walk_forward_allocation(sr, cfg)
    rebs = result.rebalances["inverse_vol"]
    assert rebs
    last = rebs[-1]
    day = pd.Timestamp(last.date)
    # What the harness SHOULD have used: strictly-before-day, tail(60).
    window = sr.returns.loc[sr.returns.index < day].tail(60)
    expected = solve_weights(
        "inverse_vol", ["sma|EURUSD|1h", "rsi|XAUUSD|4h"], window,
        min_window_observations=cfg.min_window_observations,
    )
    assert expected is not None
    expected_map = dict(zip(["sma|EURUSD|1h", "rsi|XAUUSD|4h"], expected))
    for sleeve, w in expected_map.items():
        assert last.weights[sleeve] == pytest.approx(w, abs=1e-12)
    # Sanity: sleeve A's calm recent regime earns it the larger weight.
    assert last.weights["sma|EURUSD|1h"] > last.weights["rsi|XAUUSD|4h"]


def test_matrix_sleeve_without_daily_return_recovers_return_from_equity(tmp_path):
    """N10: when ``daily_return`` is absent the return is recovered from the
    SAME sleeve's equity column — not silently zero."""
    csv_path = tmp_path / "matrix_daily_equity.csv"
    days = pd.bdate_range("2024-01-01", periods=10)
    equities = [100000.0, 101000.0, 100000.0, 102000.0] + [102000.0] * 6
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        import csv as csv_mod

        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy", "symbol", "timeframe", "run_id", "time",
                "equity", "daily_return",
            ],
        )
        writer.writeheader()
        for day, equity in zip(days, equities):
            writer.writerow(
                {
                    "strategy": "sma", "symbol": "EURUSD", "timeframe": "1h",
                    "run_id": "r1", "time": day.isoformat(), "equity": equity,
                    "daily_return": "",
                }
            )
    result = load_sleeve_returns(matrix_csv=csv_path)
    series = result.returns["sma|EURUSD|1h"].dropna()
    assert series.loc[pd.Timestamp("2024-01-02")] == pytest.approx(0.01)
    assert series.loc[pd.Timestamp("2024-01-03")] == pytest.approx(100000 / 101000 - 1.0)
    assert series.loc[pd.Timestamp("2024-01-04")] == pytest.approx(0.02)


def test_overlapping_walkforward_folds_keep_the_later_fold():
    """N9: duplicate return dates across folds resolve keep='last' — the
    later (more recent) fold wins."""
    payload = {
        "mode": "strategy", "strategy": "x.py", "symbols": ["EURUSD"],
        "timeframe": "1h",
        "folds": [
            {
                "fold": 1, "test_start": "2023-01-02", "test_end": "2023-01-04",
                "oos_stats": {"total_trades": 5},
                "oos_equity_curve": [
                    {"time": "2023-01-02T00:00:00Z", "equity": 100000.0},
                    {"time": "2023-01-03T00:00:00Z", "equity": 110000.0},
                ],
            },
            {
                "fold": 2, "test_start": "2023-01-03", "test_end": "2023-01-05",
                "oos_stats": {"total_trades": 5},
                "oos_equity_curve": [
                    {"time": "2023-01-03T00:00:00Z", "equity": 200000.0},
                    {"time": "2023-01-04T00:00:00Z", "equity": 204000.0},
                ],
            },
        ],
    }
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wf_overlap.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = load_sleeve_returns(walkforward_paths=[path])
    series = result.returns["x|EURUSD|1h"].dropna()
    # 2023-01-03 appears in both folds: fold1 says +10%, fold2 anchors at 0.
    # keep='last' -> fold2's 0.0 must win.
    assert series.loc[pd.Timestamp("2023-01-03")] == pytest.approx(0.0)
    assert series.loc[pd.Timestamp("2023-01-04")] == pytest.approx(0.02)


def test_first_fold_day_return_is_not_dropped():
    """Nit: the first observed day's return measures close(day1)/first raw
    equity point - the fold's starting mark - not nothing."""
    curve = [
        {"time": "2023-01-02T09:30:00Z", "equity": 100000.0},  # fold start mark
        {"time": "2023-01-02T16:00:00Z", "equity": 101000.0},  # day-1 close
        {"time": "2023-01-03T16:00:00Z", "equity": 102000.0},
    ]
    returns = _oos_fold_returns(curve)
    # Day 1 return = 101000/100000 - 1 (was previously dropped entirely).
    # The fold series keeps tz-aware UTC stamps straight from the payload.
    assert returns.loc[pd.Timestamp("2023-01-02", tz="UTC")] == pytest.approx(0.01)
    assert returns.loc[pd.Timestamp("2023-01-03", tz="UTC")] == pytest.approx(
        102000 / 101000 - 1.0
    )


def test_ruin_floor_boundary_is_inclusive():
    """Equity min == ruin_equity_floor rejects (<=, fail closed)."""
    index = pd.bdate_range("2021-01-04", periods=100)
    # Floor 0.5 keeps the boundary exactly representable: 1 + (-0.5) == 0.5.
    edge = np.full(100, 0.001)
    edge[0] = -0.5    # equity -> exactly 0.5 == floor
    safe = np.full(100, 0.001)
    safe[0] = -0.499  # equity -> 0.501 > floor
    df = pd.DataFrame({"edge|X|1d": edge, "safe|X|1d": safe}, index=index)
    sr = align_sleeve_returns(
        {c: df[c] for c in df.columns},
        metadata={c: _meta(c) for c in df.columns},
    )
    result = filter_eligible_sleeves(
        sr, EligibilityConfig(min_history_days=0, ruin_equity_floor=0.5)
    )
    assert "ruined" in result.rejected["edge|X|1d"]
    assert "safe|X|1d" in result.eligible


def test_inverse_vol_uses_sample_std_ddof1_and_min_window_boundary():
    """Pins ddof=1 and the ``min_window_observations`` boundary: sleeves with
    different observation counts only diverge under the right ddof."""
    index = pd.bdate_range("2024-01-01", periods=130)
    rng = np.random.default_rng(9)
    a = np.concatenate([rng.normal(0.0, 0.01, 120), np.full(10, np.nan)])  # 120 obs
    b = np.full(130, np.nan)
    b[:30] = rng.normal(0.0, 0.02, 30)      # exactly min_window=30 obs
    window = pd.DataFrame({"a|X|1d": a, "b|X|1d": b}, index=index)
    ids = ["a|X|1d", "b|X|1d"]
    w = solve_weights("inverse_vol", ids, window, min_window_observations=30)
    assert w is not None
    vol_a = float(np.std(a[np.isfinite(a)], ddof=1))
    vol_b = float(np.std(b[:30], ddof=1))
    expected = np.array([1 / vol_a, 1 / vol_b])
    expected /= expected.sum()
    assert w == pytest.approx(expected, abs=1e-12)
    # One observation short of the boundary -> zero weight.
    c = np.full(130, np.nan)
    c[:29] = rng.normal(0.0, 0.02, 29)
    window2 = pd.DataFrame({"a|X|1d": a, "c|X|1d": c}, index=index)
    w2 = solve_weights("inverse_vol", ["a|X|1d", "c|X|1d"], window2, min_window_observations=30)
    assert w2 is not None
    assert w2[1] == 0.0
    assert w2[0] == pytest.approx(1.0)


def test_report_discloses_sleeve_sources_and_matrix_oos_warning():
    """Should-fix 5: the report records each sleeve's source and warns that
    matrix sleeves are not verified out-of-sample."""
    df = _synthetic_returns(n_days=300)
    sr = _synthetic_sleeve_returns(df)
    meta = dict(sr.metadata)
    for sid in meta:
        meta[sid] = SleeveMetadata(
            **{**meta[sid].__dict__, "source": "matrix"}
        )
    sr_matrix = SleeveReturns(returns=sr.returns, metadata=meta)
    result = walk_forward_allocation(
        sr_matrix, AllocationConfig(lookback_days=60, objectives=("equal_weight",))
    )
    assert result.report["sleeves"]["sources"]
    assert all(v == "matrix" for v in result.report["sleeves"]["sources"].values())
    assert any("NOT verified out-of-sample" in n for n in result.report["notes"])


def test_cli_writes_report_to_tmp_path(tmp_path, capsys):
    """CLI smoke test: end-to-end run writing JSON under tmp_path."""
    import csv as csv_mod

    from runner.sleeve_allocator.cli import main

    csv_path = tmp_path / "matrix_daily_equity.csv"
    days = pd.bdate_range("2024-01-01", periods=45)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy", "symbol", "timeframe", "run_id", "time",
                "equity", "daily_return",
            ],
        )
        writer.writeheader()
        for i, day in enumerate(days):
            writer.writerow(
                {
                    "strategy": "sma", "symbol": "EURUSD", "timeframe": "1h",
                    "run_id": "r1", "time": day.isoformat(),
                    "equity": 100000 * (1.0005**i), "daily_return": 0.0005,
                }
            )
    out = tmp_path / "report.json"
    rc = main(
        [
            "--matrix-csv", str(csv_path),
            "--matrix-is-oos",
            "--output", str(out),
            "--lookback-days", "10",
            "--min-history-days", "0",
            "--min-trades", "0",
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload["objectives"]) == {"equal_weight", "inverse_vol"}
    assert payload["inputs"]["matrix_is_oos_acknowledged"] is True
    assert payload["sleeves"]["sources"]["sma|EURUSD|1h"] == "matrix"
    assert any("NOT verified out-of-sample" in n for n in payload["notes"])


def test_cli_requires_matrix_is_oos_acknowledgement(tmp_path):
    """--matrix-csv without --matrix-is-oos must refuse: the curves are
    full-window backtests the loader cannot verify."""
    import csv as csv_mod

    from runner.sleeve_allocator.cli import main

    csv_path = tmp_path / "matrix_daily_equity.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy", "symbol", "timeframe", "run_id", "time",
                "equity", "daily_return",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "strategy": "sma", "symbol": "EURUSD", "timeframe": "1h",
                "run_id": "r1", "time": "2024-01-02", "equity": 100000.0,
                "daily_return": 0.0,
            }
        )
    out = tmp_path / "report.json"
    with pytest.raises(SystemExit):
        main(["--matrix-csv", str(csv_path), "--output", str(out)])
    assert not out.exists()


# ---------------------------------------------------------------------------
# Review bundle 32: metric correctness, eligibility boundaries, loader edges
# ---------------------------------------------------------------------------


def test_curve_metrics_hand_computed_values():
    """Every headline metric hand-computed on a small deterministic series."""
    from runner.sleeve_allocator import curve_metrics

    index = pd.bdate_range("2024-01-01", periods=60)
    returns = pd.Series(
        [0.01, -0.005] * 30, index=index, dtype=float, name="p"
    )
    metrics = curve_metrics(returns)

    equity = (1.0 + returns).cumprod()
    expected_final = float(equity.iloc[-1])
    assert metrics["final_equity"] == pytest.approx(expected_final)
    assert metrics["total_return"] == pytest.approx(expected_final - 1.0)

    # Measured density: 60 returns over 60 business days (~84 calendar days).
    span_days = (index[-1] - index[0]).days + 1
    ppy = 60 * 365.25 / span_days
    assert metrics["ann_return"] == pytest.approx(
        expected_final ** (ppy / 60) - 1.0
    )
    std = float(returns.std(ddof=1))
    mean = float(returns.mean())
    assert metrics["ann_volatility"] == pytest.approx(std * np.sqrt(ppy))
    assert metrics["sharpe"] == pytest.approx(mean / std * np.sqrt(ppy))

    peak = equity.cummax()
    drawdown = ((peak - equity) / peak).max()
    assert metrics["max_drawdown"] == pytest.approx(float(drawdown))
    assert metrics["n_days"] == 60


def test_curve_metrics_known_ten_percent_year_reports_ten_percent():
    """Blocker 1: a business-day curve compounding to exactly +10%/yr
    (261 observations over ~1y, the align_sleeve_returns calendar) must
    report ~10%, not the sqrt(252)-vs-261-row 9.64% artefact."""
    from runner.sleeve_allocator import curve_metrics

    index = pd.bdate_range("2023-01-02", "2023-12-29")
    n = len(index)
    daily = 1.10 ** (1.0 / n) - 1.0
    returns = pd.Series([daily] * n, index=index, dtype=float)
    metrics = curve_metrics(returns)
    assert metrics["total_return"] == pytest.approx(0.10, abs=1e-9)
    # abs=2e-3: the old sqrt(252) formula reports 0.0964 (fails); measured
    # density reports ~0.1001 (passes).
    assert metrics["ann_return"] == pytest.approx(0.10, abs=2e-3)


def test_curve_metrics_empty_and_singleton_series():
    from runner.sleeve_allocator import curve_metrics

    empty = curve_metrics(pd.Series(dtype=float))
    assert empty["n_days"] == 0
    assert empty["ann_return"] is None and empty["sharpe"] is None

    single = curve_metrics(
        pd.Series([0.01], index=pd.DatetimeIndex(["2024-01-02"]))
    )
    assert single["n_days"] == 1
    assert single["sharpe"] is None and single["ann_volatility"] is None
    assert single["ann_return"] == pytest.approx(
        1.01 ** (252.0 / 1) - 1.0
    )  # unmeasurable density -> 252 fallback


def test_eligibility_min_history_days_boundary_is_inclusive():
    """min_history_days is a >= threshold: span == threshold admits."""
    start = "2022-01-03"  # Monday
    end = pd.Timestamp(start) + pd.Timedelta(days=730)
    index = pd.bdate_range(start, end)
    span = int((index[-1] - index[0]).days)
    df = pd.DataFrame({"a|X|1d": np.full(len(index), 0.001)}, index=index)
    sr = align_sleeve_returns({"a|X|1d": df["a|X|1d"]}, metadata={"a|X|1d": _meta("a|X|1d")})
    cfg = EligibilityConfig(min_history_days=span, min_trades=0)
    assert "a|X|1d" in filter_eligible_sleeves(sr, cfg).eligible
    cfg_strict = EligibilityConfig(min_history_days=span + 1, min_trades=0)
    assert "insufficient_history" in filter_eligible_sleeves(
        sr, cfg_strict
    ).rejected["a|X|1d"]


def test_eligibility_min_trades_boundary_is_inclusive():
    index = pd.bdate_range("2021-01-04", periods=400)
    df = pd.DataFrame({"a|X|1d": np.full(400, 0.001)}, index=index)
    periods = (TradePeriod(start="2020-01-01", end="2024-12-31", trades=100),)
    sr = align_sleeve_returns(
        {"a|X|1d": df["a|X|1d"]},
        metadata={"a|X|1d": _meta("a|X|1d", trade_periods=periods)},
    )
    cfg = EligibilityConfig(min_history_days=0, min_trades=100)
    assert "a|X|1d" in filter_eligible_sleeves(sr, cfg).eligible
    cfg_strict = EligibilityConfig(min_history_days=0, min_trades=101)
    assert "insufficient_trades" in filter_eligible_sleeves(
        sr, cfg_strict
    ).rejected["a|X|1d"]


def test_eligibility_min_coverage_rejects_sparse_sleeve():
    """min_coverage is the observed/calendar fraction of the shared frame."""
    index = pd.bdate_range("2021-01-04", periods=400)
    dense = np.full(400, 0.001)
    sparse = np.full(400, np.nan)
    sparse[:100] = 0.001  # only 25% of the calendar observed
    df = pd.DataFrame({"dense|X|1d": dense, "sparse|X|1d": sparse}, index=index)
    sr = align_sleeve_returns(
        {c: df[c] for c in df.columns},
        metadata={c: _meta(c) for c in df.columns},
    )
    cfg = EligibilityConfig(min_history_days=0, min_trades=0, min_coverage=0.5)
    result = filter_eligible_sleeves(sr, cfg)
    assert "insufficient_coverage" in result.rejected["sparse|X|1d"]
    assert "dense|X|1d" in result.eligible


def test_harness_admits_rebalance_when_window_equals_lookback():
    """A rebalance is NOT skipped when the lookback window is exactly full:
    the skip condition is ``len(window) < lookback``, so equality admits."""
    rng = np.random.default_rng(4)
    index = pd.bdate_range("2021-01-04", periods=560)
    df = pd.DataFrame(
        {"a|X|1d": rng.normal(0.0005, 0.005, 560), "b|X|1d": rng.normal(0, 0.01, 560)},
        index=index,
    )
    sr = align_sleeve_returns(
        {c: df[c] for c in df.columns},
        metadata={c: _meta(c) for c in df.columns},
    )
    cfg = AllocationConfig(
        lookback_days=252,
        objectives=("equal_weight",),
        eligibility=EligibilityConfig(min_history_days=0, min_trades=0),
    )
    result = walk_forward_allocation(sr, cfg)
    # Early month-ends have < 252 prior observations and are skipped; the
    # first month-end with exactly 252 prior business days must be admitted
    # (not skipped for being one short).
    assert result.report["skipped_rebalances"] >= 1
    assert result.rebalances["equal_weight"], "expected at least one rebalance"


def test_loader_dedup_suffix_when_matrix_and_walkforward_share_id(tmp_path):
    """A matrix sleeve and a walk-forward sleeve with the same
    strategy|symbol|timeframe get deterministic ~N suffixes."""
    csv_path = tmp_path / "m.csv"
    days = pd.bdate_range("2024-01-01", periods=5)
    import csv as csv_mod

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy", "symbol", "timeframe", "run_id", "time",
                "equity", "daily_return",
            ],
        )
        writer.writeheader()
        for day in days:
            writer.writerow(
                {
                    "strategy": "sma", "symbol": "EURUSD", "timeframe": "1h",
                    "run_id": "r1", "time": day.isoformat(),
                    "equity": 100000.0, "daily_return": 0.001,
                }
            )
    wf = {
        "mode": "strategy", "strategy": "sma", "symbols": ["EURUSD"],
        "timeframe": "1h",
        "folds": [{
            "fold": 1, "test_start": "2024-02-01", "test_end": "2024-02-02",
            "oos_stats": {"total_trades": 7},
            "oos_equity_curve": [
                {"time": "2024-02-01T00:00:00Z", "equity": 100000.0},
                {"time": "2024-02-02T00:00:00Z", "equity": 101000.0},
            ],
        }],
    }
    wf_path = tmp_path / "wf.json"
    wf_path.write_text(json.dumps(wf), encoding="utf-8")

    result = load_sleeve_returns(matrix_csv=csv_path, walkforward_paths=[wf_path])
    assert set(result.returns.columns) == {"sma|EURUSD|1h", "sma|EURUSD|1h~2"}
    assert result.metadata["sma|EURUSD|1h"].source == "matrix"
    assert result.metadata["sma|EURUSD|1h~2"].source == "walkforward"
    assert result.metadata["sma|EURUSD|1h~2"].extra["deduplicated_from"] == "sma|EURUSD|1h"


def test_loader_drops_business_days_where_every_sleeve_is_nan():
    """A business day inside the span with no observation for ANY sleeve is
    dropped from the calendar rather than published as an all-NaN row."""
    # Two disjoint observation windows: gap of business days in between.
    first = pd.bdate_range("2024-01-01", periods=5)
    second = pd.bdate_range("2024-02-01", periods=5)
    index = first.append(second)
    series = pd.Series(np.full(len(index), 0.001), index=index)
    result = align_sleeve_returns({"a|X|1d": series})
    # Only the 10 observed business days survive; the January gap days and
    # the weekend are gone.
    assert len(result.returns) == 10
    assert result.returns.index.equals(pd.DatetimeIndex(sorted(index)))


def test_loader_duplicate_dates_keep_last_value():
    index = pd.to_datetime(
        ["2024-01-02", "2024-01-02", "2024-01-03"], utc=True
    )
    series = pd.Series([0.01, 0.05, 0.02], index=index)
    result = align_sleeve_returns({"a|X|1d": series})
    out = result.returns["a|X|1d"].dropna()
    assert out.loc[pd.Timestamp("2024-01-02")] == pytest.approx(0.05)
    assert out.loc[pd.Timestamp("2024-01-03")] == pytest.approx(0.02)


def test_cli_with_no_inputs_errors(tmp_path):
    from runner.sleeve_allocator.cli import main

    out = tmp_path / "report.json"
    with pytest.raises(SystemExit):
        main(["--output", str(out)])
    assert not out.exists()


def test_walkforward_loader_numeric_time_is_unix_seconds(tmp_path):
    """runner.walkforward emits int Unix-second `time` values; they must not
    collapse to 1970 via pandas' nanosecond default."""
    t0 = int(pd.Timestamp("2023-01-02", tz="UTC").timestamp())
    t1 = int(pd.Timestamp("2023-01-03", tz="UTC").timestamp())
    payload = {
        "mode": "strategy", "strategy": "x.py", "symbols": ["EURUSD"],
        "timeframe": "1h",
        "folds": [{
            "fold": 1, "test_start": "2023-01-02", "test_end": "2023-01-03",
            "oos_stats": {"total_trades": 2},
            "oos_equity_curve": [
                {"time": t0, "equity": 100000.0},
                {"time": t1, "equity": 101000.0},
            ],
        }],
    }
    path = tmp_path / "wf_unix.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = load_sleeve_returns(walkforward_paths=[path])
    assert not result.returns.empty
    sleeve = result.returns["x|EURUSD|1h"].dropna()
    assert sleeve.index.min() >= pd.Timestamp("2023-01-01")
    assert sleeve.loc[pd.Timestamp("2023-01-03")] == pytest.approx(0.01)


def test_trade_counts_override_is_authoritative_over_fold_periods(tmp_path):
    """An explicit trade_counts override replaces the walk-forward fold
    trade_periods: eligibility sees ONE total_trades basis, so under as_of
    the sleeve is rejected as full-sample rather than silently keeping the
    per-period counts the override was meant to replace."""
    payload = {
        "mode": "strategy", "strategy": "x.py", "symbols": ["EURUSD"],
        "timeframe": "1h",
        "folds": [{
            "fold": 1, "test_start": "2023-01-02", "test_end": "2023-06-30",
            "oos_stats": {"total_trades": 500},
            "oos_equity_curve": [
                {"time": "2023-01-02T00:00:00Z", "equity": 100000.0},
                {"time": "2023-06-30T00:00:00Z", "equity": 101000.0},
            ],
        }],
    }
    path = tmp_path / "wf_override.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # Without override: periods basis -> counted point-in-time.
    plain = load_sleeve_returns(walkforward_paths=[path])
    assert plain.metadata["x|EURUSD|1h"].trade_periods

    # With override: periods gone, single total_trades basis.
    over = load_sleeve_returns(
        walkforward_paths=[path], trade_counts={"x|EURUSD|1h": 42}
    )
    meta = over.metadata["x|EURUSD|1h"]
    assert meta.trade_periods == ()
    assert meta.total_trades == 42

    cfg = EligibilityConfig(min_history_days=0, min_trades=1)
    as_of = pd.Timestamp("2023-07-15")
    strict = filter_eligible_sleeves(over, cfg, as_of=as_of)
    assert "trades_unknown_as_of" in strict.rejected["x|EURUSD|1h"]


def test_align_sleeve_returns_rejects_non_datetime_index():
    with pytest.raises(ValueError, match="datetime index"):
        align_sleeve_returns({"a|X|1d": pd.Series([0.01, 0.02])})


def test_matrix_summary_unreadable_logs_warning(tmp_path, caplog):
    import logging

    csv_path = tmp_path / "m.csv"
    import csv as csv_mod

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy", "symbol", "timeframe", "run_id", "time",
                "equity", "daily_return",
            ],
        )
        writer.writeheader()
        for day in pd.bdate_range("2024-01-01", periods=3):
            writer.writerow(
                {
                    "strategy": "sma", "symbol": "EURUSD", "timeframe": "1h",
                    "run_id": "r1", "time": day.isoformat(),
                    "equity": 100000.0, "daily_return": 0.001,
                }
            )
    bad = tmp_path / "summary.json"
    bad.write_text("not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="runner.sleeve_allocator.loader"):
        result = load_sleeve_returns(matrix_csv=csv_path, matrix_summary=bad)
    assert any("unreadable" in r.message for r in caplog.records)
    assert result.metadata["sma|EURUSD|1h"].total_trades is None


def test_oos_fold_returns_leaves_post_gap_day_unobserved():
    """A gap containing a trading weekday must not fold a multi-day move
    into a one-day return: the first post-gap day stays unobserved,
    mirroring runner.walkforward's OOS aggregation."""
    curve = [
        # Thu -> Fri: consecutive trading days, ordinary one-day move.
        {"time": "2024-01-04T16:00:00Z", "equity": 100000.0},
        {"time": "2024-01-05T16:00:00Z", "equity": 101000.0},
        # Fri -> Tue: Monday 2024-01-08 was a trading day with no
        # observation, so the Tue "return" spans four calendar days.
        {"time": "2024-01-09T16:00:00Z", "equity": 103000.0},
        # Tue -> Fri: Wednesday/Thursday were trading days -> masked too.
        {"time": "2024-01-12T16:00:00Z", "equity": 104000.0},
        # Fri -> Mon across a pure weekend: still one trading move.
        {"time": "2024-01-15T16:00:00Z", "equity": 105000.0},
        # Jan -> Jul: a long trading-day gap -> masked.
        {"time": "2024-07-03T16:00:00Z", "equity": 106000.0},
        # Wed Jul 3 -> Fri Jul 5 2024: Thursday was a US equity holiday,
        # so the gap is weekend/holiday only and the move is kept.
        {"time": "2024-07-05T16:00:00Z", "equity": 107000.0},
    ]
    returns = _oos_fold_returns(curve)
    tz = "UTC"
    # Every gap containing a trading weekday is dropped entirely.
    for masked in ("2024-01-09", "2024-01-12", "2024-07-03"):
        assert pd.Timestamp(masked, tz=tz) not in returns.index
    # Weekend-only and holiday-only gaps keep their close-to-close move.
    assert returns.loc[pd.Timestamp("2024-01-15", tz=tz)] == pytest.approx(
        105000 / 104000 - 1.0
    )
    assert returns.loc[pd.Timestamp("2024-07-05", tz=tz)] == pytest.approx(
        107000 / 106000 - 1.0
    )
    # Sanity: the ordinary move before the gap is untouched.
    assert returns.loc[pd.Timestamp("2024-01-05", tz=tz)] == pytest.approx(0.01)


def test_harness_filters_sleeves_without_window_observations():
    """A sleeve eligible on history/trades but with (almost) no observations
    inside the estimation window is excluded at that rebalance, so
    equal_weight and inverse_vol always see the same universe."""
    df = _synthetic_returns(n_days=800)
    dead_col = "dead|X|1d"
    rng = np.random.default_rng(11)
    df[dead_col] = np.where(
        df.index < df.index[400],
        rng.normal(0.001, 0.002, len(df)),
        np.nan,
    )
    sr = _synthetic_sleeve_returns(df)

    cfg = AllocationConfig(
        lookback_days=252,
        min_window_observations=30,
        objectives=("equal_weight", "inverse_vol"),
        eligibility=EligibilityConfig(min_history_days=0, min_trades=0),
    )
    result = walk_forward_allocation(sr, cfg)

    dead_active = [
        r for r in result.rebalances["equal_weight"]
        if r.weights.get(dead_col, 0.0) > 0.0
    ]
    dead_filtered = [
        r
        for r in result.rebalances["equal_weight"]
        if dead_col in r.weights and r.weights[dead_col] == 0.0
    ]
    assert dead_active, "expected early rebalances where the dead sleeve traded"
    assert dead_filtered, "expected late rebalances where it is window-filtered"
    # Both objectives see the same universe: inverse_vol holds the sleeve
    # while it is window-eligible and drops it exactly when equal_weight does.
    inv_by_date = {r.date: r for r in result.rebalances["inverse_vol"]}
    for rec in dead_active:
        assert inv_by_date[rec.date].weights.get(dead_col, 0.0) > 0.0
    for rec in dead_filtered:
        assert inv_by_date[rec.date].weights.get(dead_col, 0.0) == 0.0
    assert "insufficient_window_observations" in result.report["sleeves"][
        "rejected_union"
    ].get(dead_col, [])


def test_cli_flags_reach_harness_config(tmp_path):
    """CLI flags must land in the report's config echo: --lookback-days and
    --min-trades were mutation survivors that changed nothing observable."""
    import csv as csv_mod

    from runner.sleeve_allocator.cli import main

    csv_path = tmp_path / "matrix_daily_equity.csv"
    days = pd.bdate_range("2024-01-01", periods=45)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.DictWriter(
            fh,
            fieldnames=[
                "strategy", "symbol", "timeframe", "run_id", "time",
                "equity", "daily_return",
            ],
        )
        writer.writeheader()
        for i, day in enumerate(days):
            writer.writerow(
                {
                    "strategy": "sma", "symbol": "EURUSD", "timeframe": "1h",
                    "run_id": "r1", "time": day.isoformat(),
                    "equity": 100000 * (1.0005**i), "daily_return": 0.0005,
                }
            )
    out = tmp_path / "report.json"
    rc = main(
        [
            "--matrix-csv", str(csv_path),
            "--matrix-is-oos",
            "--output", str(out),
            "--lookback-days", "10",
            "--min-history-days", "0",
            "--min-trades", "7",
            "--ruin-equity-floor", "0.05",
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    config = payload["config"]
    assert config["lookback_days"] == 10
    elig = config["eligibility"]
    assert elig["min_history_days"] == 0
    assert elig["min_trades"] == 7
    assert elig["ruin_equity_floor"] == pytest.approx(0.05)
    for name in ("equal_weight", "inverse_vol"):
        obj = payload["objectives"][name]
        assert obj["n_rebalances"] >= 1
        assert "cash_rebalances" in obj


def test_trade_count_basis_disclosed_when_min_trades_zero():
    """With min_trades=0 the trade-count check is skipped but the basis is
    still recorded, so the report can disclose full-sample counting."""
    df = _synthetic_returns(n_days=100)
    sr = align_sleeve_returns(
        {c: df[c] for c in df.columns},
        metadata={c: _meta(c, total_trades=500) for c in df.columns},
    )
    result = filter_eligible_sleeves(
        sr, EligibilityConfig(min_history_days=0, min_trades=0)
    )
    assert result.rejected == {}
    assert result.trade_count_basis["sma|EURUSD|1h"] == "full_sample"


def test_walkforward_fold_without_trade_stats_is_unknown_not_zero(tmp_path):
    """A fold whose oos_stats reports no total_trades must surface as
    trades_unknown, not as a zero-trade period reading
    insufficient_trades."""
    payload = {
        "mode": "strategy", "strategy": "x.py", "symbols": ["EURUSD"],
        "timeframe": "1h",
        "folds": [{
            "fold": 1, "test_start": "2023-01-02", "test_end": "2023-06-30",
            "oos_stats": {},
            "oos_equity_curve": [
                {"time": "2023-01-02T00:00:00Z", "equity": 100000.0},
                {"time": "2023-06-30T00:00:00Z", "equity": 101000.0},
            ],
        }],
    }
    path = tmp_path / "wf_nostats.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = load_sleeve_returns(walkforward_paths=[path])
    meta = result.metadata["x|EURUSD|1h"]
    assert meta.trade_periods[0].trades is None
    assert meta.total_trades is None

    cfg = EligibilityConfig(min_history_days=0, min_trades=1)
    verdict = filter_eligible_sleeves(result, cfg)
    assert "trades_unknown" in verdict.rejected["x|EURUSD|1h"]


def test_walkforward_loader_malformed_json_names_the_file(tmp_path):
    bad = tmp_path / "wf_broken.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="wf_broken.json"):
        load_sleeve_returns(walkforward_paths=[bad])
