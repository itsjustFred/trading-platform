"""Tests for data_tools/resample.py."""

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from data_tools.resample import resample_ohlcv, resample_file


@pytest.fixture
def sample_1m_df():
    """Create a sample 1-minute OHLCV DataFrame."""
    idx = pd.date_range("2024-01-02 09:00", periods=120, freq="min")
    import numpy as np
    np.random.seed(42)
    opens = 100 + np.cumsum(np.random.randn(120) * 0.1)
    df = pd.DataFrame({
        "timestamp": idx,
        "open": opens,
        "high": opens + np.abs(np.random.randn(120) * 0.2),
        "low": opens - np.abs(np.random.randn(120) * 0.2),
        "close": opens + np.random.randn(120) * 0.1,
        "volume": np.random.randint(100, 10000, 120),
    })
    return df


@pytest.fixture
def sample_1h_df():
    """Create a sample 1-hour OHLCV DataFrame."""
    idx = pd.date_range("2024-01-01", periods=500, freq="h")
    import numpy as np
    np.random.seed(42)
    opens = 100 + np.cumsum(np.random.randn(500) * 0.5)
    df = pd.DataFrame({
        "timestamp": idx,
        "open": opens,
        "high": opens + np.abs(np.random.randn(500) * 1.0),
        "low": opens - np.abs(np.random.randn(500) * 1.0),
        "close": opens + np.random.randn(500) * 0.5,
        "volume": np.random.randint(1000, 100000, 500),
    })
    return df


class TestResampleOHLCV:
    def test_1m_to_5m(self, sample_1m_df):
        result = resample_ohlcv(sample_1m_df, "5m")
        # 120 minutes / 5 = 24 bars
        assert len(result) == 24
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_1m_to_15m(self, sample_1m_df):
        result = resample_ohlcv(sample_1m_df, "15m")
        assert len(result) == 8  # 120 / 15

    def test_1h_to_4h(self, sample_1h_df):
        result = resample_ohlcv(sample_1h_df, "4h")
        assert len(result) > 0
        assert len(result) <= len(sample_1h_df) // 4 + 1

    def test_ohlcv_rules(self, sample_1m_df):
        """Verify OHLCV aggregation rules: O=first, H=max, L=min, C=last, V=sum."""
        result = resample_ohlcv(sample_1m_df, "5m")
        first_5 = sample_1m_df.iloc[:5]
        bar = result.iloc[0]
        assert bar["open"] == first_5["open"].iloc[0], "Open should be first"
        assert bar["high"] == first_5["high"].max(), "High should be max"
        assert bar["low"] == first_5["low"].min(), "Low should be min"
        assert bar["close"] == first_5["close"].iloc[-1], "Close should be last"
        assert bar["volume"] == first_5["volume"].sum(), "Volume should be sum"

    def test_preserves_timestamp_column(self, sample_1m_df):
        result = resample_ohlcv(sample_1m_df, "5m")
        assert "timestamp" in result.columns
        assert result["timestamp"].is_monotonic_increasing

    def test_invalid_timeframe(self, sample_1m_df):
        with pytest.raises(ValueError):
            resample_ohlcv(sample_1m_df, "99x")


class TestResampleFile:
    def test_resample_file(self, sample_1h_df, tmp_path):
        """Test resampling a parquet file."""
        src = tmp_path / "1h.parquet"
        sample_1h_df.to_parquet(src, index=False)

        outputs = resample_file(src, ["4h"])
        assert len(outputs) == 1
        assert outputs[0].exists()
        result = pd.read_parquet(outputs[0])
        assert len(result) > 0
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_resample_file_multiple_timeframes(self, sample_1h_df, tmp_path):
        """Test resampling to multiple timeframes."""
        src = tmp_path / "1h.parquet"
        sample_1h_df.to_parquet(src, index=False)

        outputs = resample_file(src, ["4h", "1d"])
        assert len(outputs) == 2
        for p in outputs:
            assert p.exists()
            df = pd.read_parquet(p)
            assert len(df) > 0


class TestResampleMerge:
    def test_resample_preserves_stored_history_not_present_in_source(self, tmp_path):
        """Regression DV-B23: resampling must not drop older stored bars."""
        source = tmp_path / "1m.parquet"
        old_source = pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=3, freq="h", tz="UTC"),
                "open": [1.0, 2.0, 3.0],
                "high": [1.1, 2.1, 3.1],
                "low": [0.9, 1.9, 2.9],
                "close": [1.05, 2.05, 3.05],
                "volume": [10, 20, 30],
            }
        )
        old_source.to_parquet(source, index=False)

        outputs = resample_file(source, ["1h"])
        stored = pd.read_parquet(outputs[0])
        assert len(stored) == 3
        assert str(stored["timestamp"].iloc[0])[:10] == "2020-01-01"

        # The source file is later truncated to recent rows only (e.g. re-download).
        new_source = old_source.iloc[[2]].copy()
        new_source.to_parquet(source, index=False)

        outputs = resample_file(source, ["1h"])
        merged = pd.read_parquet(outputs[0])

        assert len(merged) == 3, f"history regressed: {len(merged)} rows"
        assert str(merged["timestamp"].iloc[0])[:10] == "2020-01-01"

    def test_resample_newer_values_win_on_duplicate_timestamps(self, tmp_path):
        source = tmp_path / "1m.parquet"
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=2, freq="h", tz="UTC"),
                "open": [1.0, 2.0],
                "high": [1.1, 2.1],
                "low": [0.9, 1.9],
                "close": [1.05, 2.05],
                "volume": [10, 20],
            }
        ).to_parquet(source, index=False)
        resample_file(source, ["1h"])

        revised = pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=1, freq="h", tz="UTC"),
                "open": [9.0],
                "high": [9.1],
                "low": [8.9],
                "close": [9.05],
                "volume": [99],
            }
        )
        revised.to_parquet(source, index=False)

        outputs = resample_file(source, ["1h"])
        merged = pd.read_parquet(outputs[0])

        assert len(merged) == 2
        assert float(merged.loc[merged["timestamp"].dt.date == pd.Timestamp("2020-01-01").date(), "close"].iloc[0]) == 9.05

    def test_resample_write_is_atomic(self, tmp_path, monkeypatch):
        source = tmp_path / "1m.parquet"
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2020-01-01", periods=2, freq="h", tz="UTC"),
                "open": [1.0, 2.0],
                "high": [1.1, 2.1],
                "low": [0.9, 1.9],
                "close": [1.05, 2.05],
                "volume": [10, 20],
            }
        ).to_parquet(source, index=False)

        observed: dict[str, Path] = {}

        real_to_parquet = pd.DataFrame.to_parquet

        def spy(self, path, *args, **kwargs):
            observed["path"] = Path(path)
            return real_to_parquet(self, path, *args, **kwargs)

        monkeypatch.setattr(pd.DataFrame, "to_parquet", spy)

        outputs = resample_file(source, ["1h"])

        assert observed["path"] != outputs[0], "resample target must be written via temp file"
        assert len(pd.read_parquet(outputs[0])) == 2
        assert not list(tmp_path.glob("*.tmp-*")), "no temp files left behind"
