"""Characterisation tests for the strategy marker-drawing tail (issue #264).

Every strategy that emits chart markers ends ``generate_signals`` with the
same tail: UTC-parse the ``timestamp`` column at entry/exit bars and append
capped ``{"type": "marker", ...}`` dicts to ``drawings``. The tail was
copy-pasted across ~17 strategy files; it is being extracted into one shared
helper (``shared.strategy_markers``) as a pure refactor.

These tests pin the FULL ``generate_signals`` output — entry/exit bar
indices, the exact ``drawings`` list and a SHA-256 over a canonical
serialization of ``indicators_dict`` — on deterministic synthetic frames, so
the extraction cannot silently change a single strategy's output. The
expected values in ``tests/fixtures/strategy_marker_tail/expected_outputs.json``
were captured from the pre-refactor implementations; they must keep matching
byte-for-byte after the helper lands.

No market data is required: every frame is synthetic and ``post_vix_dip_buy``
gets its VIX series monkeypatched (it is fail-closed without one).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "strategy_marker_tail"
    / "expected_outputs.json"
)


def _load_strategy_module(relative: str, name: str):
    path = PROJECT_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Deterministic synthetic frames (no randomness leaks: every rng is seeded)
# ---------------------------------------------------------------------------


def _ohlc_from_close(close):
    c = np.asarray(close, float)
    return c * 0.999, c * 1.002, c * 0.998, c


def _mkdf(ts, o, h, lo, c, vol=1000.0):
    c = np.asarray(c, float)
    return pd.DataFrame(
        {
            "timestamp": pd.DatetimeIndex(ts),
            "open": np.asarray(o, float),
            "high": np.asarray(h, float),
            "low": np.asarray(lo, float),
            "close": c,
            "volume": np.full(len(c), float(vol)),
        }
    )


def _trend_frame(rows: int = 900, start: str = "2022-01-01") -> pd.DataFrame:
    """Alternating drift regimes -> entries and exits for entry/exit tails."""
    rng = np.random.RandomState(7)
    closes = []
    price = 100.0
    for i in range(rows):
        drift = 0.6 if (i // 60) % 2 == 0 else -0.6
        price += drift + rng.normal(0, 0.15)
        closes.append(price)
    ts = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    o, h, lo, c = _ohlc_from_close(closes)
    return _mkdf(ts, o, h, lo, c)


def _updipcrash_frame(rows: int = 700, start: str = "2023-01-01") -> pd.DataFrame:
    closes = []
    price = 100.0
    for i in range(rows):
        if 300 <= i < 340:
            price -= 0.8
        elif i >= 650:
            price -= 3.0
        else:
            price += 0.5
        closes.append(price)
    ts = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    o, h, lo, c = _ohlc_from_close(closes)
    return _mkdf(ts, o, h, lo, c)


def _crash_recover_frame(rows: int = 260, start: str = "2023-01-01") -> pd.DataFrame:
    closes = []
    price = 100.0
    for i in range(rows):
        if 150 <= i < 156:
            price *= 0.97
        elif i >= 156:
            price += 0.4
        else:
            price += 0.05
        closes.append(price)
    ts = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    o, h, lo, c = _ohlc_from_close(closes)
    return _mkdf(ts, o, h, lo, c)


def _gap_frame(rows: int = 200, start: str = "2023-01-01") -> pd.DataFrame:
    """Flat closes with two large overnight gaps (mean_reversion_rsi_bb)."""
    closes = [100.0 + (i % 7) * 0.01 for i in range(rows)]
    opens = [base + (30.0 if i in (50, 120) else 0.0) for i, base in enumerate(closes)]
    ts = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    df = _mkdf(ts, closes, closes, closes, closes)
    df["open"] = opens
    df["high"] = np.maximum(df["open"], df["close"]) + 0.5
    df["low"] = np.minimum(df["open"], df["close"]) - 0.5
    return df


def _hourly_trend_frame(rows: int = 3000, start: str = "2024-01-01") -> pd.DataFrame:
    rng = np.random.RandomState(11)
    closes = 100 + np.cumsum(rng.normal(0.05, 0.4, rows))
    ts = pd.date_range(start, periods=rows, freq="h", tz="UTC")
    o, h, lo, c = _ohlc_from_close(closes)
    return _mkdf(ts, o, h, lo, c)


def _random_walk_frame(rows: int = 800, seed: int = 16, start: str = "2024-01-01") -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    closes = 100 + np.cumsum(rng.normal(0, 1.2, rows))
    ts = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    o, h, lo, c = _ohlc_from_close(closes)
    return _mkdf(ts, o, h, lo, c)


def _supertrend_frame(rows: int = 600) -> pd.DataFrame:
    """Crash -> rally -> decline: two supertrend flips for pine_supertrend_dema."""
    c = np.full(rows, 100.0)
    c[150] = 92.0
    for i in range(220, 420):
        c[i] = 92.0 + (i - 220) * 0.6
    for i in range(420, 600):
        c[i] = 212.0 - (i - 420) * 1.5
    c = c.astype(float)
    ts = pd.date_range("2024-01-01", periods=rows, freq="D", tz="UTC")
    o, h, lo, cl = _ohlc_from_close(c)
    return _mkdf(ts, o, h, lo, cl)


def _us100_frame(days: int = 3) -> pd.DataFrame:
    """Three ET days of 5-minute bars covering the 00:00-16:00 ET windows."""
    ts = pd.date_range("2024-03-04 05:00", periods=days * 276, freq="5min", tz="UTC")
    rng = np.random.RandomState(1)
    closes = 100 + np.cumsum(rng.normal(0.02, 0.3, len(ts)))
    o, h, lo, c = _ohlc_from_close(closes)
    return _mkdf(ts, o, h, lo, c)


def _ivb_frame() -> pd.DataFrame:
    """Same breakout-bar recipe the defect-golden ivb tests use."""
    breakout_bar = 3 * 24 * 4 + 66
    rows = breakout_bar + 20
    idx = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")
    close = np.full(rows, 100.0)
    high = np.full(rows, 100.2)
    low = np.full(rows, 99.8)
    open_ = np.full(rows, 100.0)
    close[breakout_bar] = 110.0
    high[breakout_bar] = 110.2
    return _mkdf(idx, open_, high, low, close)


def _post_vix_frame():
    """Six ET days of 15-minute bars + a synthetic VIX series.

    Day 3 (index 3) is the panic day: SPX -2% with VIX 22 -> 25 (+13.6%);
    days 4-5 stay in-regime and rally through the ORB window.
    """
    days = [d for d in pd.date_range("2024-03-04", periods=8, freq="D") if d.dayofweek < 5]
    frames = []
    for di, d in enumerate(days):
        t0 = pd.Timestamp(d) + pd.Timedelta(hours=13, minutes=30)
        ts = pd.date_range(t0, periods=30, freq="15min", tz="UTC")
        if di == 3:
            base = np.linspace(100, 98.0, 30)
        elif di > 3:
            base = np.full(30, 98.5)
            base[14:] = np.linspace(98.5, 101.0, 16)
        else:
            base = np.full(30, 100.0)
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": ts,
                    "open": base * 0.999,
                    "high": base * 1.001,
                    "low": base * 0.999,
                    "close": base,
                    "volume": np.full(30, 1000.0),
                }
            )
        )
    df = pd.concat(frames, ignore_index=True)
    vix_vals = [22.0] * len(days)
    vix_vals[3] = 25.0
    vix_vals[4] = 25.5
    vix_vals[5] = 26.0
    vix = pd.Series(
        vix_vals, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    )
    return df, vix


# ---------------------------------------------------------------------------
# Case table: strategy -> (frame builder, params, needs_vix_patch)
# ---------------------------------------------------------------------------

_TREND_STRATEGIES = [
    "channel_breakout",
    "cross_asset_momentum",
    "ema_crossoiver_conversion_test",
    "gold_momentum",
    "risk_parity",
    "sector_rotation",
    "stat_arb_pairs",
]


def _ivb_params(module) -> dict:
    params = dict(module.STRATEGY_META["parameters"])
    params.update(
        {
            "use_candle_close": True,
            "confirmation_timeframe": "15m",
            "entry_time": "16:30",
            "max_entry_time": "18:05",
            "exit_time": "22:55",
            "atr_period": 1,
            "atr_long_period": 1,
            "atr_regime_factor": 2.0,
        }
    )
    return params


_POST_VIX_PARAMS = {
    "use_htf_filter": False,
    "use_near_support": False,
    "use_orb_mode": True,
    "rsi_oversold": 95.0,
    "profit_pct": 5.0,
    "stop_pct": 10.0,
    "active_days_max": 10,
    "sma_period": 5,
    "rsi_period": 3,
}


def _cases() -> dict:
    """strategy name -> (frame builder, static params or callable, vix)."""
    cases = {name: (_trend_frame, {}, False) for name in _TREND_STRATEGIES}
    cases.update(
        {
            "trend_following": (_updipcrash_frame, {}, False),
            "short_term_reversal": (_crash_recover_frame, {}, False),
            "mean_reversion_rsi_bb": (_gap_frame, {}, False),
            "volatility_breakout": (_hourly_trend_frame, {}, False),
            "qg_30min_es_breakout_strategy": (_hourly_trend_frame, {}, False),
            "pine_superosc_vumanchu": (_random_walk_frame, {}, False),
            "pine_supertrend_dema": (
                _supertrend_frame,
                {"atr_period": 10, "atr_mult": 1.5, "dema_length": 9},
                False,
            ),
            "us100_scalper": (_us100_frame, {"reference_price": 100.0}, False),
            "intraday_volatility_breakout": (_ivb_frame, "MODULE_PARAMS", False),
            "post_vix_dip_buy": (_post_vix_frame, _POST_VIX_PARAMS, True),
        }
    )
    return cases


# ---------------------------------------------------------------------------
# Expected-output comparison
# ---------------------------------------------------------------------------


def _canon(value):
    """Deterministic JSON-able form of an indicators_dict value."""
    if isinstance(value, pd.Series):
        return {
            "__series__": [
                None if isinstance(x, float) and np.isnan(x) else x
                for x in value.tolist()
            ]
        }
    if isinstance(value, tuple):
        return [_canon(x) for x in value]
    if isinstance(value, list):
        return [_canon(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, float) and np.isnan(value):
        return None
    return value


def _indicators_sha256(indicators: dict) -> str:
    payload = json.dumps(
        _canon(dict(indicators)), sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


@pytest.mark.parametrize("strategy", sorted(_cases()))
def test_strategy_output_matches_pre_refactor_golden(strategy, expected):
    """generate_signals output must be identical before/after the tail refactor.

    Compares the exact entry/exit bar indices, the full drawings list and a
    SHA-256 over the canonical indicators serialization against values
    captured from the pre-refactor code.
    """
    frame_builder, params, needs_vix = _cases()[strategy]
    module = _load_strategy_module(
        f"strategies/{strategy}/{strategy}.py", f"marker_tail_{strategy}"
    )
    if needs_vix:
        df, vix = _post_vix_frame()
        module._load_vix_data = lambda start=None, end=None, v=vix: v
    else:
        df = frame_builder()
    if params == "MODULE_PARAMS":
        params = _ivb_params(module)

    entries, exits, indicators, drawings = module.generate_signals(df, dict(params))

    exp = expected[strategy]
    assert [int(i) for i in entries[entries].index.tolist()] == exp["entries"], (
        f"{strategy}: entry bars changed"
    )
    assert [int(i) for i in exits[exits].index.tolist()] == exp["exits"], (
        f"{strategy}: exit bars changed"
    )
    assert drawings == exp["drawings"], f"{strategy}: drawings changed"
    assert _indicators_sha256(indicators) == exp["indicators_sha256"], (
        f"{strategy}: indicators_dict changed"
    )
