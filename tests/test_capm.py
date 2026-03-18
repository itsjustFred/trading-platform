from __future__ import annotations

import pandas as pd
import pytest

from runner.capm import align_return_series, calculate_capm


def test_calculate_capm_linear_relationship():
    index = pd.date_range("2024-01-01", periods=8, freq="D", tz="UTC")
    benchmark = pd.Series([0.012, -0.006, 0.008, 0.003, -0.004, 0.007, 0.001, -0.002], index=index)
    strategy = 0.0005 + benchmark * 1.5

    capm = calculate_capm(strategy, benchmark)

    assert capm["beta"] == pytest.approx(1.5, rel=1e-6)
    assert capm["alpha"] == pytest.approx(0.0005 * 252, rel=1e-6)
    assert capm["r_squared"] == pytest.approx(1.0, rel=1e-6)
    assert capm["p_value"] < 1e-6


def test_align_return_series_uses_intersection():
    strategy_index = pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC")
    benchmark_index = pd.date_range("2024-01-03", periods=6, freq="D", tz="UTC")
    strategy = pd.Series([0.01, -0.01, 0.015, 0.0, 0.004, 0.002], index=strategy_index)
    benchmark = pd.Series([0.02, -0.005, 0.01, 0.003, -0.001, 0.006], index=benchmark_index)

    aligned = align_return_series(strategy, benchmark)

    assert len(aligned) == 4
    assert aligned.index.min() == pd.Timestamp("2024-01-03T00:00:00+00:00")
    assert aligned.index.max() == pd.Timestamp("2024-01-06T00:00:00+00:00")


def test_calculate_capm_requires_two_common_observations():
    strategy = pd.Series([0.01], index=[pd.Timestamp("2024-01-01T00:00:00+00:00")])
    benchmark = pd.Series([0.02], index=[pd.Timestamp("2024-01-02T00:00:00+00:00")])

    with pytest.raises(ValueError, match="At least two aligned return observations"):
        calculate_capm(strategy, benchmark)
