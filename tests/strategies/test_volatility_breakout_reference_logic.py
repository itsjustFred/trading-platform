import pandas as pd

from strategies.volatility_breakout.volatility_breakout import generate_signals


def _bars() -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-02 16:00", periods=12, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 100.0, 100.0, 100.0, 100.0, 101.8, 101.9, 100.7, 98.0, 97.8, 97.6, 100.0],
            "high": [100.2, 100.3, 100.4, 100.2, 100.4, 102.2, 102.0, 101.0, 98.2, 98.0, 97.8, 100.2],
            "low": [99.8, 99.7, 99.6, 99.8, 99.6, 101.4, 101.2, 100.3, 97.5, 97.2, 97.0, 99.8],
            "close": [100.0, 100.0, 100.0, 100.0, 100.0, 101.8, 101.6, 100.6, 98.0, 97.6, 97.4, 100.0],
            "volume": [1000] * 12,
        }
    )


def test_reference_defaults_enter_only_during_window_and_emit_direction_and_stop():
    entries, exits, indicators, _ = generate_signals(_bars(), {})

    entry_indices = entries[entries].index.tolist()

    assert entry_indices == [5, 8]
    assert indicators["direction"].iloc[5] == "LONG"
    assert indicators["direction"].iloc[8] == "SHORT"
    assert pd.notna(indicators["sl_price"].iloc[5])
    assert pd.notna(indicators["stop_loss"].iloc[8])
    assert exits.any()
