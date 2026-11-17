"""#129 / H8 residual: risk-parity scoring runs on DAILY close-to-close returns.

Sharpe is annualised with sqrt(252) everywhere else in the platform
(``runner/stats.py``, ``runner/portfolio_optimizer.py``), which is only valid
for daily observations. The risk-parity universe score used to apply sqrt(252)
to whatever bar frequency it was handed (e.g. 1h), mis-scaling Sharpe and the
universe ranking. When the frame carries no datetime index the observation
frequency is unknown, so Sharpe is reported as undefined rather than invented.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from runner import optimizer as optimizer_module
from runner.risk_parity_optimizer import (
    daily_returns_frame,
    select_best_risk_parity_universe,
)

BARS_PER_DAY = 6
SYMBOLS = ["SPY", "TLT", "GLD"]


def _daily_returns(days: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(129)
    data = {
        "SPY": rng.normal(0.0008, 0.011, days),
        "TLT": rng.normal(0.0002, 0.006, days) - 0.2 * rng.normal(0, 0.011, days),
        "GLD": rng.normal(0.0004, 0.008, days),
    }
    index = pd.date_range("2024-01-01", periods=days, freq="D", tz="UTC")
    return pd.DataFrame(data, index=index)


def _as_intraday(daily: pd.DataFrame, *, bars: int = BARS_PER_DAY) -> pd.DataFrame:
    """Split each daily return into ``bars`` bars that compound back to it exactly."""
    rng = np.random.default_rng(7)
    frames = []
    for day, row in daily.iterrows():
        bar_returns = rng.normal(0.0, 0.002, size=(bars - 1, len(row)))
        last = (1.0 + row.to_numpy()) / np.prod(1.0 + bar_returns, axis=0) - 1.0
        values = np.vstack([bar_returns, last])
        index = pd.date_range(day + pd.Timedelta(hours=14), periods=bars, freq="h")
        frames.append(pd.DataFrame(values, index=index, columns=daily.columns))
    return pd.concat(frames)


def test_hourly_and_daily_delivery_of_same_returns_score_identically() -> None:
    daily = _daily_returns()
    hourly = _as_intraday(daily)

    from_daily = select_best_risk_parity_universe(daily, [SYMBOLS, SYMBOLS[:2]])
    from_hourly = select_best_risk_parity_universe(hourly, [SYMBOLS, SYMBOLS[:2]])

    assert from_hourly["best_sharpe"] == pytest.approx(from_daily["best_sharpe"], rel=1e-9)
    assert from_hourly["best_score"] == pytest.approx(from_daily["best_score"], rel=1e-9)
    assert from_hourly["best_volatility"] == pytest.approx(
        from_daily["best_volatility"], rel=1e-9
    )
    for a, b in zip(from_daily["evaluated_universes"], from_hourly["evaluated_universes"]):
        assert a["symbols"] == b["symbols"]
        assert a["score"] == pytest.approx(b["score"], rel=1e-9)
        assert a["sharpe"] == pytest.approx(b["sharpe"], rel=1e-9)


def test_universe_ranking_is_independent_of_bar_frequency() -> None:
    daily = _daily_returns()
    hourly = _as_intraday(daily)
    universes = [SYMBOLS, ["SPY", "TLT"], ["SPY", "GLD"], ["TLT", "GLD"]]

    ranked_daily = [
        u["symbols"]
        for u in select_best_risk_parity_universe(daily, universes)["evaluated_universes"]
    ]
    ranked_hourly = [
        u["symbols"]
        for u in select_best_risk_parity_universe(hourly, universes)["evaluated_universes"]
    ]

    assert ranked_hourly == ranked_daily


def test_sharpe_is_annualised_daily_sqrt_252() -> None:
    daily = _daily_returns()
    result = select_best_risk_parity_universe(_as_intraday(daily), [SYMBOLS])

    weights = np.array([result["best_weights"][s] for s in SYMBOLS])
    portfolio = daily[SYMBOLS].to_numpy() @ weights
    expected = float(np.mean(portfolio) / (np.std(portfolio) + 1e-12) * math.sqrt(252))

    assert result["best_sharpe"] == pytest.approx(expected, rel=1e-9)
    assert result["best_volatility"] == pytest.approx(np.std(portfolio), rel=1e-6)


def test_weights_are_computed_on_daily_covariance() -> None:
    daily = _daily_returns()
    from_daily = select_best_risk_parity_universe(daily, [SYMBOLS])
    from_hourly = select_best_risk_parity_universe(_as_intraday(daily), [SYMBOLS])

    for symbol in SYMBOLS:
        assert from_hourly["best_weights"][symbol] == pytest.approx(
            from_daily["best_weights"][symbol], abs=1e-6
        )


def test_days_without_bars_are_not_zero_return_days() -> None:
    """Weekend/holiday gaps in intraday data must not add fabricated 0% days."""
    daily = _daily_returns()
    daily = daily[daily.index.dayofweek < 5]  # drop weekends entirely
    hourly = _as_intraday(daily)

    from_daily = select_best_risk_parity_universe(daily, [SYMBOLS])
    from_hourly = select_best_risk_parity_universe(hourly, [SYMBOLS])

    assert from_hourly["best_sharpe"] == pytest.approx(from_daily["best_sharpe"], rel=1e-9)


def _weekly_returns(weeks: int = 40) -> pd.DataFrame:
    """Weekly bars: one observation per week, so 52 periods a year, not 252."""
    rng = np.random.default_rng(521)
    data = {
        "SPY": rng.normal(0.005, 0.024, weeks),
        "TLT": rng.normal(0.001, 0.014, weeks) - 0.2 * rng.normal(0, 0.024, weeks),
        "GLD": rng.normal(0.002, 0.017, weeks),
    }
    index = pd.date_range("2024-01-01", periods=weeks, freq="7D", tz="UTC")
    return pd.DataFrame(data, index=index)


def _structure_score(selection: dict) -> float:
    """The scale-free terms of the universe score (no Sharpe contribution)."""
    return (
        0.35 * selection["best_diversification_ratio"]
        - 0.5 * selection["best_avg_abs_correlation"]
        - 2.0
        * sum(
            (value - 1.0 / len(SYMBOLS)) ** 2
            for value in selection["best_risk_contributions"].values()
        )
    )


def test_weekly_bars_do_not_claim_a_daily_annualisation() -> None:
    """#129 H8: weekly bars resample to one row per week.

    Applying sqrt(252) to them overstates Sharpe by sqrt(252/52) ~ 2.2x - the
    very mis-scaling this daily-resample change exists to remove - so the
    observation frequency is reported as not-daily and the Sharpe stays
    undefined, exactly like the no-datetime-index case.
    """
    weekly = _weekly_returns()

    assert daily_returns_frame(weekly) is None

    from_weekly = select_best_risk_parity_universe(weekly, [SYMBOLS])

    assert from_weekly["best_sharpe"] is None
    assert from_weekly["score_basis"] == "structure_only"
    assert from_weekly["best_score"] == pytest.approx(_structure_score(from_weekly), rel=1e-9)

    # Nothing is invented: the same weekly returns without their timestamps
    # (frequency unknown) score identically, and the raw weekly returns - not
    # pseudo-daily rows - are what the covariance sees.
    from_indexless = select_best_risk_parity_universe(weekly.reset_index(drop=True), [SYMBOLS])
    assert from_weekly["best_score"] == pytest.approx(from_indexless["best_score"], rel=1e-9)
    assert from_weekly["best_weights"] == pytest.approx(from_indexless["best_weights"], rel=1e-9)
    assert from_weekly["best_volatility"] == pytest.approx(
        from_indexless["best_volatility"], rel=1e-9
    )


def test_daily_returns_frame_drops_empty_days() -> None:
    """Days without bars are dropped, never zero-filled: a fabricated 0% day
    dilutes volatility and biases Sharpe (and with it the universe ranking)."""
    daily = _daily_returns()
    daily = daily[daily.index.dayofweek < 5]  # weekdays only
    hourly = _as_intraday(daily)

    frame = daily_returns_frame(hourly)

    assert frame is not None
    assert not any(day.dayofweek >= 5 for day in frame.index)
    assert len(frame) == len(daily)
    assert list(frame.columns) == list(daily.columns)

    weights = np.full(len(SYMBOLS), 1.0 / len(SYMBOLS))
    resampled_returns = frame.to_numpy(dtype=float) @ weights
    daily_returns_array = daily.to_numpy(dtype=float) @ weights
    assert float(np.std(resampled_returns)) == pytest.approx(
        float(np.std(daily_returns_array)), rel=1e-9
    )
    assert float(np.mean(resampled_returns)) == pytest.approx(
        float(np.mean(daily_returns_array)), rel=1e-9
    )


def test_daily_returns_frame_returns_none_without_datetime_index() -> None:
    """The guard itself: no datetime index means no known observation grid."""
    assert daily_returns_frame(_daily_returns().reset_index(drop=True)) is None


def test_frame_without_datetime_index_leaves_sharpe_undefined() -> None:
    """Unknown observation frequency: no invented annualisation."""
    frame = _daily_returns().reset_index(drop=True)

    result = select_best_risk_parity_universe(frame, [SYMBOLS])

    assert result["best_sharpe"] is None
    assert result["score_basis"] == "structure_only"
    assert sum(result["best_weights"].values()) == pytest.approx(1.0)
    entry = result["evaluated_universes"][0]
    assert entry["sharpe"] is None
    # Score is the scale-free structure terms only (no Sharpe contribution).
    expected = (
        0.35 * result["best_diversification_ratio"]
        - 0.5 * result["best_avg_abs_correlation"]
        - 2.0 * sum((v - 1.0 / 3.0) ** 2 for v in result["best_risk_contributions"].values())
    )
    assert result["best_score"] == pytest.approx(expected, rel=1e-9)


def test_datetime_frame_reports_sharpe_basis() -> None:
    result = select_best_risk_parity_universe(_daily_returns(), [SYMBOLS])

    assert result["score_basis"] == "sharpe_and_structure"
    assert result["best_sharpe"] is not None


def test_trade_pnl_fallback_frame_has_no_datetime_index() -> None:
    """Per-trade rows are not a time grid: the fallback frame drops the index."""
    trades = []
    for symbol, offset in (("SPY", 0.0), ("TLT", 1.0)):
        for i in range(4):
            trades.append(
                SimpleNamespace(
                    symbol=symbol,
                    entry_time=f"2024-01-0{i + 1}T10:00:00+00:00",
                    pnl=100.0 * (i + 1) - offset * 30.0,
                )
            )
    result = SimpleNamespace(
        trades=trades, meta=SimpleNamespace(initial_capital=100_000.0)
    )

    frame = optimizer_module._asset_returns_frame_from_base_result(result)

    assert list(frame.columns) == ["SPY", "TLT"]
    assert not isinstance(frame.index, pd.DatetimeIndex)

    selection = select_best_risk_parity_universe(frame, [["SPY", "TLT"]])
    assert selection["best_sharpe"] is None
    assert selection["score_basis"] == "structure_only"
