"""Hand-computed edge cases of the three published-strategy ports (no engine, no data files)."""
from __future__ import annotations

import importlib.util

import numpy as np
import pandas as pd
import pytest

from tests.parity.published_harness import REPO_ROOT


def _load(name: str, folder: str, file: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "strategies" / folder / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


faber = _load("faber_t", "faber_tactical_asset_allocation", "faber_tactical_asset_allocation.py")
connors = _load("connors_t", "connors_rsi2_mean_reversion", "connors_rsi2_mean_reversion.py")
gem = _load("gem_t", "antonacci_global_equity_momentum", "gem_dual_momentum.py")


def _daily(closes, start="2020-01-01"):
    ts = pd.bdate_range(start, periods=len(closes), tz="UTC")
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c, "low": c, "close": c, "volume": 1.0})


# ── Faber ────────────────────────────────────────────────────────────────


def test_faber_needs_ten_month_ends_before_any_signal():
    df = _daily(np.linspace(100, 300, 8 * 22))  # < 10 calendar months of rising prices
    entries, exits, *_ = faber.generate_signals(df, {"sma_months": 10})
    assert not entries.any() and not exits.any()


def test_faber_enters_on_month_end_above_sma_and_exits_below():
    # 22 bdays per block: rise 12 months, then crash
    closes = np.concatenate([np.full(22, 100.0 + 10 * m) for m in range(12)] + [np.full(22, 50.0)] * 2)
    df = _daily(closes)
    entries, exits, ind, _ = faber.generate_signals(df, {"sma_months": 10})
    ts = df["timestamp"]
    month_end = ind["Month_End"]
    assert set(ts[entries].dt.strftime("%Y-%m")) <= set(ts[month_end].dt.strftime("%Y-%m"))
    assert entries.sum() == 1 and exits.sum() == 1
    assert df.index[entries][0] < df.index[exits][0]


def test_faber_last_bar_is_a_month_end():
    df = _daily(np.arange(1, 50, dtype=float))
    flags = faber.month_end_flags(df["timestamp"])
    assert bool(flags.iloc[-1])


# ── Connors ──────────────────────────────────────────────────────────────


def test_wilder_rsi_degenerate_cases():
    up = connors.wilder_rsi(pd.Series([1.0, 2.0, 3.0, 4.0]), 2)
    down = connors.wilder_rsi(pd.Series([4.0, 3.0, 2.0, 1.0]), 2)
    flat = connors.wilder_rsi(pd.Series([5.0, 5.0, 5.0, 5.0]), 2)
    assert up.iloc[-1] == 100.0 and down.iloc[-1] == 0.0
    assert pd.isna(flat.iloc[-1])
    assert up.iloc[:2].isna().all()  # warm-up: period changes consumed


def test_wilder_rsi_hand_value():
    # changes +1, -1: avg gain = avg loss = 0.5 -> RSI 50; then +2: ag=(0.5+2)/2=1.25, al=0.25 -> 100-100/6
    rsi = connors.wilder_rsi(pd.Series([10.0, 11.0, 10.0, 12.0]), 2)
    assert rsi.iloc[2] == pytest.approx(50.0)
    assert rsi.iloc[3] == pytest.approx(100.0 - 100.0 / 6.0)


def test_connors_single_position_and_exit_above_sma5():
    base = np.linspace(100, 200, 260)
    closes = base.copy()
    closes[250:253] = [190.0, 180.0, 170.0]  # sharp 3-day pullback in an uptrend: RSI(2) < 10
    closes[253:] = np.linspace(172, 195, 7)
    df = _daily(closes)
    entries, exits, *_ = connors.generate_signals(df, {})
    assert entries.sum() >= 1
    assert entries.sum() - exits.sum() in (0, 1)  # never two open at once


# ── GEM ──────────────────────────────────────────────────────────────────


def _monthly(us, non_us, bond, cash):
    idx = pd.period_range("2020-01", periods=len(us), freq="M")
    return pd.DataFrame({"us": us, "non_us": non_us, "bond": bond, "cash": cash}, index=idx, dtype=float)


def test_gem_allocation_branches():
    n = 14
    flat = [100.0] * n
    rising = [100.0 + 5 * i for i in range(n)]
    steeper = [100.0 + 10 * i for i in range(n)]
    cash = [100.0 + 0.1 * i for i in range(n)]
    params = {"lookback_months": 12}
    assert gem.gem_allocation(_monthly(steeper, rising, flat, cash), params).iloc[-1] == "us"
    assert gem.gem_allocation(_monthly(rising, steeper, flat, cash), params).iloc[-1] == "non_us"
    assert gem.gem_allocation(_monthly(flat, flat, rising, [100.0 + 5 * i for i in range(n)]), params).iloc[-1] == "bond"
    # tie between US and non-US with both above cash goes to non-US (>=), as in the source
    assert gem.gem_allocation(_monthly(rising, rising, flat, cash), params).iloc[-1] == "non_us"
    # lookback not yet available -> no allocation
    assert pd.isna(gem.gem_allocation(_monthly(rising, rising, flat, cash), params).iloc[5])


def test_gem_peer_columns_are_lower_case_because_engines_lowercase_columns():
    cols = gem.peer_close_columns({"us_symbol": "SPY", "non_us_symbol": "VEU", "bond_symbol": "AGG", "cash_symbol": "BIL"})
    assert all(name == name.lower() for name in cols.values())


def test_gem_refuses_to_guess_slot_or_missing_peer():
    df = _daily(np.linspace(100, 120, 30))
    with pytest.raises(ValueError, match="slot"):
        gem.generate_signals(df, {})
    df["slot"] = "us"
    with pytest.raises(ValueError, match="peer"):
        gem.generate_signals(df, {})


def test_gem_attach_rejects_unconfigured_symbol():
    with pytest.raises(ValueError, match="not a configured GEM slot"):
        gem.attach_peer_closes({"XYZ": _daily([1.0, 2.0])}, {})
