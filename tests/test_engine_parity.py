"""Cross-engine parity on a deterministic bundled fixture.

Decision Record (Cleanup 17, 2026-08-31):
- On a deterministic bundled fixture, native vs vectorbt engine parity uses
  EXACT equality of trade count (and of the per-trade prices/timestamps).
- On real/local data, divergence must be at most one trade; a test that
  cannot promise that is a smoke test and must be named accordingly.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter

STRATEGY_PATH = str(
    Path(__file__).resolve().parents[1] / "strategies" / "example_sma" / "example_sma.py"
)

PARITY_START = "2024-01-01"
PARITY_END = "2024-12-31"


def _deterministic_ohlcv() -> pd.DataFrame:
    """A fully deterministic trend/consolidation series.

    Alternating 40-bar up-trends (slope +0.5/bar) and 40-bar flat phases
    produce several SMA(5)/SMA(20) crossover events without any random
    component; the final declining phase forces a cross-down so the last
    trade closes before end-of-data.
    """
    closes: list[float] = []
    price = 100.0
    for block in range(4):
        if block % 2 == 0:
            for _ in range(40):
                price += 0.5
                closes.append(price)
        else:
            for _ in range(40):
                closes.append(price)
    for _ in range(60):
        price -= 0.5
        closes.append(price)
    n = len(closes)
    timestamps = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.25 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    )
    return frame


@pytest.fixture(scope="module")
def parity_data_dir(tmp_path_factory) -> Path:
    """Bundled fixture written into a tmp ohlcv tree (no env dependence)."""
    data_dir = tmp_path_factory.mktemp("parity-data")
    symbol_dir = data_dir / "ohlcv" / "PARITY"
    symbol_dir.mkdir(parents=True)
    _deterministic_ohlcv().to_parquet(symbol_dir / "1h.parquet", index=False)
    return data_dir


def _parity_config(data_dir: Path) -> BacktestConfig:
    return BacktestConfig(risk_per_trade_pct=0.01,
        strategy_name="example_sma",
        symbols=["PARITY"],
        timeframe="1h",
        start_date=PARITY_START,
        end_date=PARITY_END,
        initial_capital=100_000,
        # Parity needs identical caps on both engines: vectorbt's position
        # cap would otherwise bind (0.2 default) while native caps at 1.0x.
        # Non-binding on both sides keeps the comparison about fills/PnL.
        max_position_exposure_pct=1.0,
        max_leverage=10.0,
        modeling_resolution="main_timeframe",
        data_dir=str(data_dir),
        parameters={"fees": "zero", "fast_length": 5, "slow_length": 20},
    )


def test_parity_fixture_itself_is_deterministic():
    first = _deterministic_ohlcv()
    second = _deterministic_ohlcv()
    pd.testing.assert_frame_equal(first, second)


def test_native_and_vectorbt_agree_exactly_on_bundled_fixture(parity_data_dir):
    """Decision Record: exact parity on the deterministic bundled fixture.

    Tolerance decision (documented per the Cleanup 17 Decision Record):
    - trade COUNT must match exactly;
    - per-trade entry price must match exactly (both engines fill the next
      bar open);
    - PnL is compared with rel=5% ONLY because the engines currently use
      different signal-exit fill conventions (see below);

    Signal-exit timing/pricing is intentionally NOT asserted as equal: the
    native engine fills a signal exit at the signal bar's close while the
    vectorbt engine shifts exits one bar and fills at the next bar's open
    (ox-alpha F10, "three execution-timing models"). On a trending fixture
    that systematically changes the exit price and therefore the PnL, so
    PnL divergence must stay within one exit bar's move (rel=5% here); the
    moment the engines share one execution contract this tolerance must be
    tightened to exact equality.
    """
    cfg = _parity_config(parity_data_dir)

    native = NativeAdapter().run(cfg, STRATEGY_PATH)
    vectorbt = VectorbtAdapter().run(cfg, STRATEGY_PATH)

    assert len(native.trades) > 0, "Fixture must produce trades for parity to be meaningful"
    assert len(native.trades) == len(vectorbt.trades), (
        f"Native produced {len(native.trades)} trades, "
        f"vectorbt produced {len(vectorbt.trades)}"
    )

    for index, (native_trade, vectorbt_trade) in enumerate(zip(native.trades, vectorbt.trades)):
        assert pd.Timestamp(native_trade.entry_time) == pd.Timestamp(vectorbt_trade.entry_time), (
            f"Trade {index}: entry time diverges between engines"
        )
        # Entry price is EXACT (rel=1e-9): both engines fill the next bar's
        # open of the same deterministic series, so any difference is a real
        # divergence, not a floating-point artifact of the tolerance decision.
        assert native_trade.entry_price == pytest.approx(vectorbt_trade.entry_price, rel=1e-9), (
            f"Trade {index}: entry price diverges between engines"
        )
        assert native_trade.pnl == pytest.approx(vectorbt_trade.pnl, rel=0.05), (
            f"Trade {index}: pnl diverges between engines "
            f"({native_trade.pnl} vs {vectorbt_trade.pnl})"
        )
