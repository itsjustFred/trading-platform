from __future__ import annotations

from pathlib import Path

import pandas as pd


def test_yfinance_download_handles_reset_index_date_column(monkeypatch, tmp_path: Path):
    from data_tools.downloaders import yfinance_dl

    index = pd.to_datetime(
        ["1990-01-02T00:00:00Z", "1990-01-03T00:00:00Z"],
        utc=True,
    )
    index.name = "Date"
    payload = pd.DataFrame(
        {
            ("Open", "AAPL"): [1.0, 2.0],
            ("High", "AAPL"): [1.5, 2.5],
            ("Low", "AAPL"): [0.5, 1.5],
            ("Close", "AAPL"): [1.2, 2.2],
            ("Volume", "AAPL"): [100.0, 150.0],
        },
        index=index,
    )

    monkeypatch.setattr(yfinance_dl.yf, "download", lambda **kwargs: payload)

    downloader = yfinance_dl.YFinanceDownloader()
    result = downloader.download(
        symbol="AAPL",
        timeframe="1d",
        output_dir=tmp_path,
        start="1990-01-01",
        end="1990-01-10",
    )

    assert result.rows == 2
    stored = pd.read_parquet(result.path)
    assert list(stored.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert str(stored["timestamp"].iloc[0]).startswith("1990-01-02")
