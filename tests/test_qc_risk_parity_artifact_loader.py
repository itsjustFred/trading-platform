from pathlib import Path

import pandas as pd
import pytest

from scripts.regenerate_qc_risk_parity_artifact import _load_local_prices
from scripts.run_risk_parity_backtest import TICKERS


def test_load_local_prices_reads_complete_local_parquet_universe(tmp_path: Path):
    source = Path("ohlcv/yfinance/equity/US")
    timestamps = pd.date_range("2020-01-01", periods=3, freq="1D", tz="UTC")
    for idx, ticker in enumerate(TICKERS):
        symbol_dir = tmp_path / source / ticker
        symbol_dir.mkdir(parents=True)
        pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100 + idx, 101 + idx, 102 + idx],
                "high": [101 + idx, 102 + idx, 103 + idx],
                "low": [99 + idx, 100 + idx, 101 + idx],
                "close": [100.5 + idx, 101.5 + idx, 102.5 + idx],
                "volume": [1000, 1000, 1000],
            }
        ).to_parquet(symbol_dir / "1d.parquet", index=False)

    prices, open_prices = _load_local_prices(tmp_path, source, "2020-01-01", "2020-01-03")

    assert list(prices.columns) == TICKERS
    assert len(prices) == 3
    assert prices["SPY"].iloc[0] == 100.5
    assert list(open_prices.columns) == TICKERS
    assert len(open_prices) == 3
    assert open_prices["SPY"].iloc[0] == 100


def test_regenerate_offline_accepts_history_start_kwarg(tmp_path: Path):
    """Offline-Regen muss den history_start-Kwarg von run_backtest (A-M6) schlucken."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.regenerate_qc_risk_parity_artifact import regenerate

    source = Path("ohlcv/yfinance/equity/US")
    timestamps = pd.date_range("2020-01-01", periods=260, freq="1D", tz="UTC")
    for idx, ticker in enumerate(TICKERS):
        symbol_dir = tmp_path / source / ticker
        symbol_dir.mkdir(parents=True)
        base = [100 + idx + 0.05 * d + 0.3 * ((d % 7) - 3) for d in range(260)]
        pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": base,
                "high": [b + 0.5 for b in base],
                "low": [b - 0.5 for b in base],
                "close": [b + 0.1 for b in base],
                "volume": [1000] * 260,
            }
        ).to_parquet(symbol_dir / "1d.parquet", index=False)

    output = tmp_path / "rp_offline.json"
    backtest = regenerate(
        data_dir=tmp_path, source=source, output=output,
        start="2020-01-01", end="2020-09-16", history_start="2015-08-04",
    )
    assert output.exists()
    assert backtest.meta.id == "rp_offline"


def test_regenerate_passes_the_parity_statistics_flags(monkeypatch, tmp_path: Path):
    """The checked-in reference note says its stats use LEAN conventions with rf=0.025.

    ``run_backtest`` defaults to ``lean_statistics=False, risk_free_rate=0.0``, so the
    regeneration must pass both explicitly or the pinned Sharpe (0.350569) is not reproduced.
    """
    import scripts.regenerate_qc_risk_parity_artifact as regen

    empty = pd.DataFrame()
    seen: dict[str, object] = {}

    def fake_run_backtest(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after run")

    monkeypatch.setattr(regen, "_load_local_prices", lambda *args: (empty, empty))
    monkeypatch.setattr(regen.risk_parity, "run_backtest", fake_run_backtest)
    with pytest.raises(RuntimeError, match="stop after run"):
        regen.regenerate(
            data_dir=tmp_path, source=Path("x"), output=tmp_path / "o.json",
            start="2016-01-01", end="2016-06-30",
        )
    assert seen["lean_statistics"] is True
    assert seen["risk_free_rate"] == 0.025


def test_regenerate_loads_prices_from_margined_history_start(monkeypatch, tmp_path: Path):
    """Offline regeneration must keep the 200-day covariance margin (A-M6)."""
    import scripts.regenerate_qc_risk_parity_artifact as regen
    from scripts.run_risk_parity_backtest import price_history_start

    seen: list[str] = []

    def stop(data_dir, source, start, end):
        seen.append(start)
        raise RuntimeError("stop after load")

    monkeypatch.setattr(regen, "_load_local_prices", stop)
    with pytest.raises(RuntimeError, match="stop after load"):
        regen.regenerate(
            data_dir=tmp_path, source=Path("x"), output=tmp_path / "o.json",
            start="2016-01-01", end="2016-06-30",
        )
    assert seen == [price_history_start("2016-01-01", "2015-08-04")] == ["2015-01-16"]
