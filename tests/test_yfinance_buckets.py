"""Bucket-convention tests for the yfinance downloader (cleanup finding DV-B20).

yfinance only offers 60m bars; the 4h output timeframe must therefore be
resampled onto the agreed UTC-aligned hourly grid (00:00 UTC origin,
Dukascopy style) and must retain the original source bucket metadata.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def _hourly_payload(start: str, hours: int) -> pd.DataFrame:
    index = pd.date_range(start=start, periods=hours, freq="1h", tz="UTC")
    index.name = "Datetime"
    return pd.DataFrame(
        {
            "Open": [1.0 + i for i in range(hours)],
            "High": [2.0 + i for i in range(hours)],
            "Low": [0.5 + i for i in range(hours)],
            "Close": [1.5 + i for i in range(hours)],
            "Volume": [10.0 + i for i in range(hours)],
        },
        index=index,
    )


def _normalized_hourly(start: str, hours: int) -> pd.DataFrame:
    """Payload as it looks after the downloader's normalization step."""
    from data_tools.downloaders import yfinance_dl

    payload = _hourly_payload(start, hours).reset_index()
    payload.columns = [str(col).strip().lower() for col in payload.columns]
    payload = payload.rename(columns={"datetime": "timestamp"})
    payload["timestamp"] = pd.to_datetime(payload["timestamp"], utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        payload[column] = pd.to_numeric(payload[column])
    return payload[yfinance_dl_keep_columns()]


def yfinance_dl_keep_columns() -> list[str]:
    return ["timestamp", "open", "high", "low", "close", "volume"]


def test_resample_4h_utc_grid_aligns_to_00_00_utc():
    from data_tools.downloaders import yfinance_dl

    payload = _normalized_hourly("2024-03-04T01:00:00Z", 7)  # 01:00 .. 07:00 UTC

    resampled, origin = yfinance_dl._resample_4h_utc_grid(payload)

    assert origin == "UTC"
    assert [ts.isoformat() for ts in resampled["timestamp"]] == [
        "2024-03-04T00:00:00+00:00",
        "2024-03-04T04:00:00+00:00",
    ]
    # First bucket spans [00:00, 04:00) UTC and holds hours 01..03:
    # open=1.0 (hour 01), close=3.5 (hour 03), volume=10+11+12+13.
    assert float(resampled.loc[0, "open"]) == 1.0
    assert float(resampled.loc[0, "close"]) == 3.5
    assert float(resampled.loc[0, "high"]) == 4.0
    assert float(resampled.loc[0, "low"]) == 0.5
    assert float(resampled.loc[0, "volume"]) == 10.0 + 11.0 + 12.0
    # 04:00-07:00 UTC bucket (partial: hours 04..06)
    assert float(resampled.loc[1, "open"]) == 4.0
    assert float(resampled.loc[1, "close"]) == 7.5
    # Original source bucket metadata is retained (decision record).
    assert set(resampled["source_bucket_timeframe"]) == {"60m"}


def test_resample_4h_utc_grid_is_dukascopy_comparable():
    """4h labels must land on the shared UTC hourly grid (00/04/08/12/16/20)."""
    from data_tools.downloaders import yfinance_dl

    df = _normalized_hourly("2024-03-04T00:00:00Z", 48)

    resampled, _ = yfinance_dl._resample_4h_utc_grid(df)

    assert len(resampled) == 12
    assert sorted({ts.hour for ts in resampled["timestamp"]}) == [0, 4, 8, 12, 16, 20]
    assert str(resampled["timestamp"].dt.tz) == "UTC"


def test_download_4h_stores_utc_aligned_buckets_and_metadata(monkeypatch, tmp_path: Path):
    from data_tools.downloaders import yfinance_dl

    payload = _hourly_payload("2024-03-04T01:00:00Z", 7)
    monkeypatch.setattr(yfinance_dl.yf, "download", lambda **kwargs: payload)

    downloader = yfinance_dl.YFinanceDownloader()
    result = downloader.download(
        symbol="AAPL",
        timeframe="4h",
        output_dir=tmp_path,
        start="2024-03-04",
        end="2024-03-05",
    )

    stored = pd.read_parquet(result.path)
    assert list(stored["timestamp"].dt.hour[:2]) == [0, 4]
    assert str(stored["timestamp"].dt.tz) == "UTC"
    assert set(stored["source_bucket_timeframe"]) == {"60m"}
    assert any("bucket" in warning.lower() for warning in result.warnings)
    assert any("UTC" in warning for warning in result.warnings)


def test_resample_4h_derives_source_bucket_from_provider_interval(
    monkeypatch, tmp_path: Path
):
    """The recorded source bucket must come from the provider interval.

    yfinance ships 60m bars for 4h today, but if the provider map changes
    (e.g. a future 4h interval or a 30m fallback), the metadata must follow
    the interval actually requested — not a hardcoded "60m".
    """
    from data_tools.downloaders import yfinance_dl

    payload = _hourly_payload("2024-03-04T01:00:00Z", 7)
    requested: list[str] = []

    def fake_download(**kwargs):
        requested.append(kwargs.get("interval"))
        return payload

    monkeypatch.setattr(yfinance_dl.yf, "download", fake_download)
    monkeypatch.setitem(yfinance_dl.YF_INTERVAL_MAP, "4h", "30m")

    downloader = yfinance_dl.YFinanceDownloader()
    result = downloader.download(
        symbol="AAPL",
        timeframe="4h",
        output_dir=tmp_path,
        start="2024-03-04",
        end="2024-03-05",
    )

    assert requested == ["30m"]
    stored = pd.read_parquet(result.path)
    assert set(stored["source_bucket_timeframe"]) == {"30m"}


def test_download_non_4h_timeframes_keep_native_buckets(monkeypatch, tmp_path: Path):
    from data_tools.downloaders import yfinance_dl

    payload = _hourly_payload("2024-03-04T01:00:00Z", 3)
    monkeypatch.setattr(yfinance_dl.yf, "download", lambda **kwargs: payload)

    downloader = yfinance_dl.YFinanceDownloader()
    result = downloader.download(
        symbol="AAPL",
        timeframe="1h",
        output_dir=tmp_path,
        start="2024-03-04",
        end="2024-03-05",
    )

    stored = pd.read_parquet(result.path)
    assert list(stored["timestamp"].dt.hour) == [1, 2, 3]
    assert "source_bucket_timeframe" not in stored.columns
