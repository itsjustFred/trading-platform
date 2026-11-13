"""
Tests fuer Strategie: Overnight Gap Mean Reversion (Daily Proxy).

Deterministische Synthetic-Frame-Tests (keine Marktdaten noetig):
- Fade-Richtung (Gap down -> LONG, Gap up -> SHORT)
- Sigma-Filter (hohes Sigma -> keine Entries)
- Exit am naechsten Bar (1-Bar-Hold als Tages-Proxy der 09:45-Liquidation)
- sizing_sl_price pro Entry vorhanden (Sizing-Level, keine Source-Regel,
  kein Live-Exit — A-M3)
- Provenance: STRATEGY_META zeigt auf overnight_gap_mean_reverse,
  keine BB/RSI/ATR-Relikte.
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "mean_reversion_rsi_bb" / "mean_reversion_rsi_bb.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mean_reversion_gap", str(STRATEGY_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gap_frame(rows: int = 80) -> pd.DataFrame:
    """Daily frame with a slow drift (tiny background gaps) and two big gaps."""
    closes = [100.0 + 0.01 * i for i in range(rows)]
    opens = list(closes)
    # Gap down of -5 at bar 65 (after close 65-1), gap up of +5 at bar 70.
    opens[65] = closes[64] - 5.0
    opens[70] = closes[69] + 5.0
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - 0.5 for o, c in zip(opens, closes, strict=True)]
    timestamps = pd.date_range("2023-01-02", periods=rows, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1_000_000.0] * rows,
        }
    )


def test_returns_four_tuple_contract():
    module = _load_module()
    entries, exits, indicators, drawings = module.generate_signals(_gap_frame(), {"sigma": 1.0})
    assert isinstance(entries, pd.Series)
    assert isinstance(exits, pd.Series)
    assert isinstance(indicators, dict)
    assert isinstance(drawings, list)


def test_fades_gap_direction_with_next_bar_exit():
    module = _load_module()
    entries, exits, indicators, _ = module.generate_signals(_gap_frame(), {"sigma": 1.0})
    entry_idx = entries[entries].index.tolist()
    assert entry_idx == [65, 70]
    assert indicators["direction"].loc[65] == "LONG"  # gap down -> long
    assert indicators["direction"].loc[70] == "SHORT"  # gap up -> short
    # Daily proxy for the 09:45 liquidate: flat one bar later.
    assert exits.iloc[66] and exits.iloc[71]
    assert indicators["exit_reason"].loc[66] == "time_exit_next_open"
    assert indicators["exit_reason"].loc[71] == "time_exit_next_open"


def test_sigma_filter_blocks_small_gaps():
    module = _load_module()
    entries, _, _, _ = module.generate_signals(_gap_frame(), {"sigma": 100.0})
    assert not entries.any(), "sigma=100 must block 5-point gaps on ~0.17 volatility"


def test_sizing_sl_price_present_at_entries_and_not_a_live_stop():
    module = _load_module()
    df = _gap_frame()
    entries, _, indicators, _ = module.generate_signals(df, {"sigma": 1.0})
    # A-M3: sizing-only key — "sl_price" must NOT be published (engines
    # would arm it as a live intrabar stop under modeling_resolution).
    assert "sl_price" not in indicators
    sl = indicators["sizing_sl_price"]
    for idx in entries[entries].index:
        assert pd.notna(sl.loc[idx]), f"missing sizing_sl_price at entry {idx}"
    # Structural sizing level: LONG stop at entry-bar low, SHORT at entry-bar high.
    assert sl.loc[65] == pytest.approx(df["low"].loc[65])
    assert sl.loc[70] == pytest.approx(df["high"].loc[70])


def test_no_bollinger_rsi_atr_remnants_and_provenance():
    module = _load_module()
    _, _, indicators, _ = module.generate_signals(_gap_frame(), {"sigma": 1.0})
    for remnant in ("RSI", "BB_Upper", "BB_Lower", "BB_Mid", "ATR"):
        assert remnant not in indicators, f"remnant indicator {remnant} still present"
    source = module.STRATEGY_META.get("source_url", "")
    assert "overnight_gap_mean_reverse" in source
    assert module.STRATEGY_META["tags"]["link"] == source
    assert "BB" not in str(module.STRATEGY_META.get("parameters", {}))
