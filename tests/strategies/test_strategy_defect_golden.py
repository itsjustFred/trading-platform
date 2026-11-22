"""Deterministic golden-master tests for the strategy-correctness defects (Cleanup #35).

Every defect fixed in this branch is pinned here against a deterministic
synthetic frame. Per Decision Record #14 (strategy functions are not changed
mechanically without parity/golden tests) each test documents the old
(pre-fix) behaviour it would have failed on, so the behavior change is
reviewable — see the golden-master diff in PR for cleanup/strategies.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_strategy_module(relative: str, name: str):
    path = PROJECT_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Deterministic frame helpers
# ---------------------------------------------------------------------------


def _trend_frame(rows: int = 260, start: str = "2024-01-01", freq: str = "1h") -> pd.DataFrame:
    """Deterministic uptrend/consolidation/decline series (no randomness)."""
    closes: list[float] = []
    price = 100.0
    for block in range(4):
        if block % 2 == 0:
            for _ in range(max(rows // 4, 1)):
                price += 0.5
                closes.append(price)
        else:
            closes.extend(price for _ in range(max(rows // 4, 1)))
    while len(closes) < rows:
        price -= 0.5
        closes.append(price)
    closes = closes[:rows]
    timestamps = pd.date_range(start, periods=rows, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.25 for c in closes],
            "close": closes,
            "volume": [1000.0] * rows,
        }
    )


def _call(module, df: pd.DataFrame, params: dict | None = None):
    return module.generate_signals(df, dict(params or {}))


# ---------------------------------------------------------------------------
# 1. Pine strategies must return the 4-tuple platform contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative,name",
    [
        ("strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py", "pine_superosc"),
        ("strategies/pine_supertrend_dema/pine_supertrend_dema.py", "pine_supertrend_dema"),
        (
            "strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py",
            "ema_conversion",
        ),
    ],
)
def test_pine_and_conversion_strategies_return_four_tuple_contract(relative, name):
    """Pre-fix these strategies returned a 3-tuple (no drawings element).

    The platform contract (vectorbt adapter ``_normalize_signal_output`` and
    native engine ``_normalize_strategy_output``) documents
    ``(entries, exits, indicators_dict, drawings)``; every sibling strategy
    returns four elements. Old behaviour: ``len(output) == 3``.
    """
    module = _load_strategy_module(relative, name)
    output = _call(module, _trend_frame())
    assert isinstance(output, tuple), "generate_signals must return a tuple"
    assert len(output) == 4, (
        "generate_signals must return the 4-tuple platform contract "
        "(entries, exits, indicators_dict, drawings)"
    )
    entries, exits, indicators, drawings = output
    assert isinstance(entries, pd.Series)
    assert isinstance(exits, pd.Series)
    assert isinstance(indicators, dict)
    assert isinstance(drawings, list)


def test_pine_strategies_4_tuple_output_flows_through_vectorbt_normalization():
    """The 4-tuple must survive the adapter's normalization unchanged.

    Old behaviour: the 3-tuple was silently patched up by the adapter's
    len-3 branch, so the contract violation was invisible to the engines.
    """
    from runner.engines.vectorbt_adapter import _normalize_signal_output

    module = _load_strategy_module(
        "strategies/pine_supertrend_dema/pine_supertrend_dema.py", "pine_supertrend_dema_norm"
    )
    df = _trend_frame()
    output = _call(module, df)
    signals, indicators, _drawings = _normalize_signal_output(
        output, df.index, symbol="TEST"
    )
    assert list(signals.columns[:2]) == ["entries", "exits"]
    assert len(signals) == len(df)
    assert set(indicators) == set(output[2])


@pytest.mark.parametrize(
    "relative",
    sorted(
        str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        for path in (PROJECT_ROOT / "strategies").rglob("*.py")
        if not path.name.startswith("__")
        and not path.stem.endswith("_context")
        and path.parent.name == path.stem
        and path.stem not in {"pynecore_orb", "pynecore_sma_crossover", "tiingo_sentiment"}
    ),
)
def test_every_strategy_module_returns_four_tuple_contract(relative):
    """Repo-wide arity contract (Pre-mortem strategies #2, PR #61).

    ``vectorbt_adapter._normalize_signal_output`` and the native engine both
    repair a len-3 output silently, so a refactor that drops ``drawings``
    from one strategy passes CI and only shows up as missing chart markers /
    empty annotation exports later. Pinning every module keeps the ambiguity
    from returning. The PyneCore scripts are excluded: they need the optional
    ``pynecore`` package and are covered by the sibling test above plus
    ``test_naming_integrity_meta_matches_implementation``.
    """
    module = _load_strategy_module(relative, "arity_" + Path(relative).stem)
    if not hasattr(module, "generate_signals"):
        pytest.skip("module exposes no generate_signals")
    output = _call(module, _trend_frame())
    assert isinstance(output, tuple), f"{relative}: generate_signals must return a tuple"
    assert len(output) == 4, (
        f"{relative}: generate_signals must return the 4-tuple platform contract "
        "(entries, exits, indicators_dict, drawings)"
    )


# ---------------------------------------------------------------------------
# 2. QG ES breakout: Pine-verified fade semantics
# ---------------------------------------------------------------------------
# The Pine source (rndm_stuff/QG-30Min_ES_Breakout_Strategy.pine, lines 58-66)
# is a FADE system, not a breakout system:
#   breakout_long  = low + FRACT * |open - low|
#   breakout_short = high - FRACT * |open - low|   (note: |open-low|, NOT |high-open|)
#   filter_long  = adx < 30   (non-trending)
#   filter_short = adx > 30   (trending)
#   enter_long   = can_trade and filter_long  and close < breakout_long
#   enter_short  = can_trade and filter_short and close < breakout_short
# A Cleanup #35 intermediate state inverted this to breakout semantics
# (long on adx > 30 & close >= level); these tests pin the Pine reading, which
# was verified against the .pine file itself rather than second-hand claims.


def _qg_module():
    return _load_strategy_module(
        "strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py", "qg_es"
    )


def _qg_fade_frame(side: str, days: int = 30) -> pd.DataFrame:
    """Deterministic NY-session frame for one fade side.

    Six bars per session (09:30-14:30 NY, inside the 08:00-15:00 window).
    ``side="long"`` is sideways (ADX stays low) with red bars whose close sits
    below ``low + |open - low|``; ``side="short"`` is a steady uptrend (ADX
    runs high) with closes below ``high - |open - low|``. The one-trade-per-day
    rule caps entries at one per session; the protective stop (entry-bar low /
    high) closes the trade the next bar, so every session after the ADX warm-up
    yields exactly one entry on its first bar.
    """
    bars_per_day = 6
    rows = days * bars_per_day
    recs: list[dict] = []
    base_ts = pd.Timestamp("2024-01-02", tz="US/Eastern").normalize().tz_convert("UTC")
    for day in range(days):
        for bar in range(bars_per_day):
            minute = 9 * 60 + 30 + bar * 60
            ts = base_ts + pd.Timedelta(days=day, minutes=minute)
            if side == "long":
                open_ = 100.3
                close = 100.0 if bar % 2 == 0 else 100.35
                high = 100.4
                low = 99.9
            else:
                base = 100.0 + day * 1.5
                open_ = base + 0.1
                close = base + 0.4
                high = base + 0.8
                low = base
            recs.append(
                {
                    "timestamp": ts,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 1000.0,
                }
            )
    return pd.DataFrame(recs)


def test_qg_enters_long_on_fade_down_when_adx_low():
    """Fade semantics: a long requires ``adx < 30`` and ``close < breakout_long``."""
    module = _qg_module()
    df = _qg_fade_frame("long")
    params = {"adx_period": 10, "adx_smooth": 3, "fract": 1.0}
    entries, _, indicators, _ = _call(module, df, params)

    assert entries.any(), "deterministic fade-down path must produce long entries"
    for entry_idx in entries[entries].index:
        long_level = float(indicators["breakout_long"].loc[entry_idx])
        assert float(df["close"].loc[entry_idx]) < long_level, (
            "every long entry must close below its breakout_long level (fade)"
        )
        assert float(indicators["ADX"].loc[entry_idx]) < 30.0, (
            "longs only trade the non-trending regime (adx < 30)"
        )
        assert indicators["direction"].loc[entry_idx] == "LONG"


def test_qg_enters_short_on_weak_close_when_adx_high():
    """Fade semantics: a short requires ``adx > 30`` and ``close < breakout_short``.

    The short level uses ``|open - low|`` per the Pine
    (``high - FRACT * |open - low|``), not ``|high - open|``.
    """
    module = _qg_module()
    df = _qg_fade_frame("short")
    params = {"adx_period": 10, "adx_smooth": 3, "fract": 1.0}
    entries, _, indicators, _ = _call(module, df, params)

    assert entries.any(), "deterministic trend path must produce short entries"
    for entry_idx in entries[entries].index:
        row = df.loc[entry_idx]
        expected_short = float(row["high"]) - 1.0 * abs(float(row["open"]) - float(row["low"]))
        assert abs(float(indicators["breakout_short"].loc[entry_idx]) - expected_short) < 1e-9, (
            "breakout_short must use |open - low| (Pine line 59)"
        )
        assert float(row["close"]) < expected_short, (
            "every short entry must close below its breakout_short level (fade)"
        )
        assert float(indicators["ADX"].loc[entry_idx]) > 30.0, (
            "shorts only trade the trending regime (adx > 30)"
        )
        assert indicators["direction"].loc[entry_idx] == "SHORT"


def test_qg_suppressed_side_never_fires_when_direction_gate_blocks_it():
    """The ADX regime gate must still suppress the opposite side.

    The choppy long frame never reaches trending ADX (no SHORTs); the trending
    short frame never drops to non-trending ADX after warm-up (no LONGs).
    """
    module = _qg_module()
    params = {"adx_period": 10, "adx_smooth": 3, "fract": 1.0}

    entries_long, _, indicators_long, _ = _call(module, _qg_fade_frame("long"), params)
    for entry_idx in entries_long[entries_long].index:
        assert indicators_long["direction"].loc[entry_idx] != "SHORT"

    entries_short, _, indicators_short, _ = _call(module, _qg_fade_frame("short"), params)
    for entry_idx in entries_short[entries_short].index:
        assert indicators_short["direction"].loc[entry_idx] != "LONG"


# ---------------------------------------------------------------------------
# 3. post_vix_dip_buy: missing VIX data must not fabricate an open gate
# ---------------------------------------------------------------------------


def _vix_module():
    return _load_strategy_module(
        "strategies/post_vix_dip_buy/post_vix_dip_buy.py", "post_vix"
    )


def _daily_frame(rows: int = 120) -> pd.DataFrame:
    rng = pd.date_range("2024-01-01", periods=rows, freq="D", tz="UTC")
    closes = 100.0 + np.sin(np.linspace(0.0, 12.0, rows)) * 5.0
    return pd.DataFrame(
        {
            "timestamp": rng,
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": [1000.0] * rows,
        }
    )


def test_post_vix_without_vix_data_emits_no_entries(monkeypatch):
    """Missing VIX data must gate the strategy OFF (fail-closed).

    Old behaviour fabricated ``vix_threshold + 1`` for every bar, i.e. the
    VIX-dip regime gate read permanently open and the strategy degraded to
    plain RSI+SMA mean reversion whenever the dataset was absent.
    """
    module = _vix_module()
    monkeypatch.setattr(module, "_load_vix_data", lambda start=None, end=None: None)

    df = _daily_frame()
    entries, _exits, indicators, _ = _call(module, df, {"sma_period": 5})

    assert not entries.any(), "no entries may be produced without VIX data"
    assert not bool(indicators["VIX_Data_Available"].iloc[0])
    assert bool(indicators["VIX_Fallback_Used"].iloc[0])
    assert indicators["VIX"].isna().all(), (
        "the fabricated VIX placeholder must not leak into the VIX indicator"
    )


def test_post_vix_uses_provided_vix_data_and_gates_entries(monkeypatch):
    """With VIX data present, entries only fire while VIX > threshold."""
    module = _vix_module()
    df = _daily_frame()
    vix = pd.Series(30.0, index=pd.DatetimeIndex(df["timestamp"]))
    monkeypatch.setattr(module, "_load_vix_data", lambda start=None, end=None: vix)

    entries, _, indicators, _ = _call(module, df, {"sma_period": 5, "rsi_period": 3})

    assert bool(indicators["VIX_Data_Available"].iloc[0])
    assert not bool(indicators["VIX_Fallback_Used"].iloc[0])
    for entry_idx in entries[entries].index:
        assert float(indicators["VIX"].loc[entry_idx]) > 20.0


def test_post_vix_date_matching_is_not_quadratic(monkeypatch):
    """VIX alignment must not rescan the whole VIX index per SPY bar.

    Old behaviour: ``vix.loc[vix.index.date == current_date]`` inside a
    Python loop over every bar — O(N x M). On a 10-year 1m frame that is
    billions of comparisons; the aligned mapping must be built once.
    """
    module = _vix_module()
    rows = 4000
    rng = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")
    closes = 100.0 + np.sin(np.linspace(0.0, 40.0, rows)) * 2.0
    df = pd.DataFrame(
        {
            "timestamp": rng,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": [1000.0] * rows,
        }
    )
    vix_dates = pd.DatetimeIndex(sorted(set(rng.normalize())))
    vix = pd.Series(30.0, index=vix_dates)
    monkeypatch.setattr(module, "_load_vix_data", lambda start=None, end=None: vix)

    import time

    started = time.perf_counter()
    _entries, _, indicators, _ = _call(module, df, {"sma_period": 5, "rsi_period": 3})
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"VIX alignment took {elapsed:.2f}s — still quadratic?"
    assert bool(indicators["VIX_Data_Available"].iloc[0])


def test_post_vix_honours_start_and_end_arguments(monkeypatch):
    """``_load_vix_data(start, end)`` must honour its range arguments.

    Old behaviour accepted the parameters and read the whole parquet.
    """
    module = _vix_module()
    captured: dict[str, object] = {}

    def fake_loader(start=None, end=None):
        captured["start"] = start
        captured["end"] = end
        return pd.Series(
            30.0, index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], tz="UTC")
        )

    monkeypatch.setattr(module, "_load_vix_data", fake_loader)
    df = _daily_frame(40)
    _call(module, df, {"sma_period": 5})

    assert captured["start"] == "2024-01-01"
    assert captured["end"] == "2024-02-09"


def test_post_vix_missing_vix_on_strategy_frame_is_reported(monkeypatch, tmp_path):
    """A VIX parquet that exists but misses the strategy window stays honest.

    The alignment must not silently forward-fill VIX values across the gap
    without surfacing which bars had no VIX observation.
    """
    module = _vix_module()
    df = _daily_frame(40)
    vix = pd.Series(
        30.0, index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], tz="UTC")
    )
    monkeypatch.setattr(module, "_load_vix_data", lambda start=None, end=None: vix)

    entries, _, indicators, _ = _call(module, df, {"sma_period": 5})

    uncovered = indicators["VIX"].isna()
    assert uncovered.iloc[-1], "bars without a VIX observation must not be fabricated"
    assert not entries[uncovered].any(), "no entry may fire on a fabricated VIX value"
    # Pre-mortem strategies #5: partial coverage must be countable, not only
    # row-by-row, so artifact reports can surface a half-covered frame even
    # when the gate never fires.
    assert indicators["VIX_Uncovered_Bars"] == int(uncovered.sum()) > 0


def test_post_vix_duplicate_vix_dates_keep_last_and_tz_naive_bars_rejected(monkeypatch):
    """Alignment must resolve duplicate VIX dates deterministically and
    reject tz-naive bar sources (Pre-mortem strategies #5).

    Duplicate dates (e.g. a corrected revision plus the original row in the
    parquet) silently pick a value; ``keep="last"`` is the documented rule.
    A naive bar index would be mis-dated against the UTC VIX series, so it
    must fail loudly instead of trading on the previous day's VIX.
    """
    module = _vix_module()

    duplicated = pd.Series(
        [25.0, 35.0], index=pd.DatetimeIndex(["2024-01-01", "2024-01-01"], tz="UTC")
    )
    df = _daily_frame(10)
    monkeypatch.setattr(module, "_load_vix_data", lambda start=None, end=None: duplicated)
    _, _, indicators, _ = _call(module, df, {"sma_period": 3})
    assert float(indicators["VIX"].iloc[0]) == 35.0, 'duplicate dates must keep "last"'

    naive = df.copy()
    naive["timestamp"] = naive["timestamp"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="tz-aware"):
        _call(module, naive, {"sma_period": 3})

    # A tz-aware but non-UTC index must be converted to UTC before deriving
    # the frame window and bar dates (CodeRabbit cycle-3 finding): local
    # 2024-01-01 20:00 America/New_York is already 2024-01-02 UTC.
    ny_index = pd.DatetimeIndex(
        ["2024-01-01 20:00", "2024-01-02 04:00", "2024-01-02 12:00"],
        tz="America/New_York",
    )
    ny_frame = pd.DataFrame(
        {
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.0] * 3,
            "close": [100.0] * 3,
        },
        index=ny_index,
    )
    captured: dict[str, object] = {}

    def loader(start=None, end=None):
        captured["start"], captured["end"] = start, end
        return pd.Series(30.0, index=pd.DatetimeIndex(["2024-01-02"], tz="UTC"))

    monkeypatch.setattr(module, "_load_vix_data", loader)
    _, _, indicators, _ = _call(module, ny_frame, {"sma_period": 3})
    assert captured["start"] == "2024-01-02"
    assert captured["end"] == "2024-01-02"
    assert indicators["VIX"].notna().all(), (
        "non-UTC tz-aware frames must align on UTC calendar dates"
    )


# ---------------------------------------------------------------------------
# 4. Signal-bots: Supertrend band recursion must not read uninitialized state
# ---------------------------------------------------------------------------


def _ramjet_module():
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    return RamJetSignalProcessor


def test_ramjet_supertrend_first_bars_are_not_green_from_zero_seed():
    """The fallback Supertrend must not report green from uninitialized state.

    Old behaviour: ``supertrend = np.ones(n_rows, dtype=bool)`` — after a
    crash below the lower band the following rows kept the inherited
    all-True seed and stayed green instead of flipping bearish.
    """
    RamJetSignalProcessor = _ramjet_module()
    rows = 30
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    # Bar 0 is a narrow calm bar; bars 1+ crash far below the lower band.
    close = np.full(rows, 100.5)
    high = np.full(rows, 101.0)
    low = np.full(rows, 100.0)
    close[1:] = 60.0
    high[1:] = 60.5
    low[1:] = 59.5
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
        }
    )

    result = RamJetSignalProcessor._calculate_supertrend(frame, period=10, multiplier=3.0)
    supertrend = result["supertrend"].astype(bool).to_numpy()

    assert not supertrend[1:8].any(), (
        f"after a crash below the lower band bars 1..7 must be bearish, got {supertrend[1:8]}"
    )


def test_ramjet_supertrend_flags_are_complete_for_every_bar():
    """Every bar must carry an explicit Supertrend decision (no NaN gate).

    Old behaviour left early bars as the all-True seed instead of an explicit
    not-yet-computable state.
    """
    RamJetSignalProcessor = _ramjet_module()
    rows = 12
    closes = np.linspace(100.0, 103.0, rows)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.4,
            "low": closes - 0.4,
            "close": closes,
        },
        index=pd.date_range("2026-01-01", periods=rows, freq="5min"),
    )
    result = RamJetSignalProcessor._calculate_supertrend(frame, period=5, multiplier=3.0)
    supertrend = result["supertrend"]
    assert len(supertrend) == rows
    assert supertrend.notna().all(), "every bar must carry an explicit Supertrend decision"


def test_ramjet_supertrend_warmup_is_flagged_not_bearish():
    """Pre-warm-up bars must be marked not-ready, not merely False.

    With the fail-closed zero seed a short frame (e.g. period=100 on a
    40-row live fetch) reads as bearish on every bar; the trailing flip
    workflow would treat that as genuine direction. ``supertrend_ready``
    separates "unknown" from "bearish" (Pre-mortem strategies #3).
    """
    RamJetSignalProcessor = _ramjet_module()
    rows = 40
    closes = np.linspace(100.0, 104.0, rows)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.4,
            "low": closes - 0.4,
            "close": closes,
        },
        index=pd.date_range("2026-01-01", periods=rows, freq="5min"),
    )
    result = RamJetSignalProcessor._calculate_supertrend(frame, period=100, multiplier=3.0)

    ready = result["supertrend_ready"].astype(bool).to_numpy()
    assert not ready.any(), (
        "a frame shorter than the Supertrend period must be marked not-ready "
        "so consumers do not read a direction into the fail-closed seed"
    )

    # A frame longer than the warm-up must become ready at the tail.
    long_closes = np.linspace(100.0, 110.0, 150)
    long_frame = pd.DataFrame(
        {
            "open": long_closes,
            "high": long_closes + 0.4,
            "low": long_closes - 0.4,
            "close": long_closes,
        },
        index=pd.date_range("2026-01-01", periods=150, freq="5min"),
    )
    long_result = RamJetSignalProcessor._calculate_supertrend(
        long_frame, period=100, multiplier=3.0
    )
    long_ready = long_result["supertrend_ready"].astype(bool).to_numpy()
    assert long_ready[-1], "the tail of a long-enough frame must be ready"
    assert not long_ready[:100].any(), (
        "the first period bars must stay not-ready"
    )


# ---------------------------------------------------------------------------
# 5. intraday_volatility_breakout: fallback must not silently switch semantics
# ---------------------------------------------------------------------------


def _ivb_module():
    return _load_strategy_module(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py", "ivb_defects"
    )


def test_ivb_candle_close_fallback_is_reported_in_indicators():
    """When the confirmation resample cannot produce signal prices, the
    strategy must mark the fallback instead of silently trading the
    ``close.shift(1)`` definition.

    Old behaviour: a bare ``except Exception`` (and the all-NaN branch) fell
    back to ``close.shift(1)`` with no marker — parity artifacts generated
    from the fallback were indistinguishable from intended behaviour.
    """
    module = _ivb_module()
    rows = 3 * 24 * 4
    idx = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
        }
    )
    params = dict(module.STRATEGY_META["parameters"])
    params["confirmation_timeframe"] = "15m"

    _entries, _exits, indicators, _ = _call(module, df, params)

    assert "signal_price_fallback_used" in indicators, (
        "the fallback must be surfaced as an indicator column"
    )
    assert "signal_price" in indicators, (
        "the price series actually used must be exposed in the indicators"
    )
    assert indicators["signal_price"].iloc[1] == pytest.approx(100.0)
    assert indicators["signal_price_fallback_used"].dtype == bool


def test_ivb_confirmation_semantics_are_stable_on_intraday_data():
    """Candle-close signals on data coarser than the confirmation TF must
    behave deterministically (documented convention, not a silent switch).

    This is the parity reference file — the triple ``.shift(-1)`` coordination
    (entries / sl_price / entry_reference_price, :414-455) is pinned here so a
    refactor cannot move one series without the other two.
    """
    module = _ivb_module()
    breakout_bar = 3 * 24 * 4 + 66  # 2024-01-04 16:30 UTC on a 15min grid
    rows = breakout_bar + 20
    idx = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")
    close = np.full(rows, 100.0)
    high = np.full(rows, 100.2)
    low = np.full(rows, 99.8)
    open_ = np.full(rows, 100.0)
    close[breakout_bar] = 110.0
    high[breakout_bar] = 110.2
    df = pd.DataFrame(
        {"timestamp": idx, "open": open_, "high": high, "low": low, "close": close}
    )
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

    entries, _exits, indicators, _ = _call(module, df, params)

    entry_indices = entries[entries].index.tolist()
    assert entry_indices == [breakout_bar], (
        "the candle-close convention emits the entry one source bar before the "
        "confirmation close it is justified by"
    )
    entry_idx = entry_indices[0]
    assert indicators["entry_reference_price"].iloc[entry_idx] == pytest.approx(
        df["close"].iloc[entry_idx]
    )
    assert indicators["order_timing"].iloc[entry_idx] == "next_bar_open"
    assert indicators["sl_price"].iloc[entry_idx] == pytest.approx(
        float(df["close"].iloc[entry_idx + 1]) - float(indicators["stop_distance"].iloc[entry_idx])
    )


def test_ivb_confirmation_fallback_contract(monkeypatch, caplog):
    """Forced-resample failure must fall back AND be visible (Pre-mortem #4).

    Pins the except-path as a contract: entries still form on the
    ``close.shift(1)`` definition, ``signal_price_fallback_used`` is True, the
    price series actually used is exposed, and the failure is logged — a
    total-fallback regression (e.g. after a pandas upgrade) must not be silent.
    """
    import logging as _logging

    module = _ivb_module()
    rows = 3 * 24 * 4
    idx = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
        }
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("resample unavailable")

    monkeypatch.setattr(pd.Series, "resample", _boom)
    with caplog.at_level(_logging.WARNING):
        _, _, indicators, _ = _call(module, df)

    fallback = indicators["signal_price_fallback_used"]
    assert fallback.astype(bool).all(), "forced resample failure must use the fallback"
    assert indicators["signal_price"].iloc[1] == pytest.approx(100.0)
    assert any(
        "close.shift(1)" in record.message and record.levelno >= _logging.WARNING
        for record in caplog.records
    ), "the fallback must be logged, not only flagged in indicators"


def test_ivb_parity_reference_frame_does_not_use_fallback():
    """The parity reference frame must run on the intended semantics.

    If the confirmation path ever degrades into the fallback on well-formed
    data (dependency drift, tz handling change), the golden reference above
    would silently pin the fallback definition instead of the candle-close
    convention. This makes that visible instead of coincidental.
    """
    module = _ivb_module()
    breakout_bar = 3 * 24 * 4 + 66
    rows = breakout_bar + 20
    idx = pd.date_range("2024-01-01", periods=rows, freq="15min", tz="UTC")
    close = np.full(rows, 100.0)
    high = np.full(rows, 100.2)
    low = np.full(rows, 99.8)
    close[breakout_bar] = 110.0
    high[breakout_bar] = 110.2
    df = pd.DataFrame(
        {"timestamp": idx, "open": close, "high": high, "low": low, "close": close}
    )
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

    _, _, indicators, _ = _call(module, df, params)

    fallback = indicators["signal_price_fallback_used"]
    assert not fallback.astype(bool).any(), (
        "the parity reference frame must run the confirmation path, not the "
        "close.shift(1) fallback — otherwise the pinned convention is the "
        "fallback, not the documented semantics"
    )


# ---------------------------------------------------------------------------
# 6. Naming integrity: meta/tags must match the implementation
# ---------------------------------------------------------------------------


def test_naming_integrity_meta_matches_implementation():
    """Meta/tags must describe what the code does (Settlement S15).

    Old behaviour: ``risk_parity`` claimed multi-asset risk parity,
    ``stat_arb_pairs`` claimed pairs trading and ``pynecore_orb`` claimed an
    opening-range breakout while implementing an SMA crossover — none
    implemented those semantics, and the tags routed discovery/portfolio
    tooling into wrong strategies. ``pynecore_orb`` was fixed back to a real
    opening-range breakout (v2.0.0), so it now claims ``breakout`` again.
    """
    risk_parity = _load_strategy_module(
        "strategies/risk_parity/risk_parity.py", "risk_parity_naming"
    )
    stat_arb = _load_strategy_module(
        "strategies/stat_arb_pairs/stat_arb_pairs.py", "stat_arb_naming"
    )
    # pynecore_orb imports the optional pynecore package; read its metadata
    # without executing the module when pynecore is not installed.
    orb_source = (PROJECT_ROOT / "strategies/pynecore_orb/pynecore_orb.py").read_text()
    orb_tree = ast.parse(orb_source)
    orb_meta = {}
    for node in orb_tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STRATEGY_META":
                    orb_meta = ast.literal_eval(node.value)

    for meta, false_claims, implementation in (
        (
            risk_parity.STRATEGY_META,
            {"risk-parity", "portfolio", "multi-asset"},
            "single-instrument",
        ),
        (stat_arb.STRATEGY_META, {"pairs-trading"}, "single-instrument"),
        # Fixed (2026-09-09): pynecore_orb implements the breakout again, so
        # the false claim under test is the leftover SMA misnomer.
        (orb_meta, {"moving-average-crossover"}, "breakout"),
    ):
        type_tags = {str(item).lower() for item in meta["tags"]["type"]}
        for claim in false_claims:
            assert claim not in type_tags, (
                f"strategy still tags itself as {claim!r} but does not implement it"
            )
        assert implementation in type_tags, (
            f"strategy must tag the implemented behavior ({implementation})"
        )
        assert "naming_note" in meta["tags"], (
            "the retained slug must be documented as a compatibility alias"
        )
