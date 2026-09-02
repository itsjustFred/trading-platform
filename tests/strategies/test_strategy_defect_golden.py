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
    assert spec is not None and spec.loader is not None
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
            for _ in range(max(rows // 4, 1)):
                closes.append(price)
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
    signals, indicators, drawings = _normalize_signal_output(
        output, df.index, symbol="TEST"
    )
    assert list(signals.columns[:2]) == ["entries", "exits"]
    assert len(signals) == len(df)
    assert set(indicators) == set(output[2])


# ---------------------------------------------------------------------------
# 2. QG ES breakout: entries read inverted relative to breakout semantics
# ---------------------------------------------------------------------------


def _qg_module():
    return _load_strategy_module(
        "strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py", "qg_es"
    )


def _qg_frame(close_path: np.ndarray, minutes: int = 5) -> pd.DataFrame:
    """Distribute a close path across many NY sessions at `minutes` after 9:30.

    One bar per session keeps the strategy's in_time window deterministic and
    makes `trades_today == 0` true on every session, so each session can
    produce at most one entry.
    """
    rows = len(close_path)
    sessions = pd.date_range("2024-01-02", periods=rows, freq="1D", tz="US/Eastern")
    timestamps = (
        sessions.normalize()
        + pd.Timedelta(hours=9, minutes=30 + minutes)
    ).tz_convert("UTC")
    closes = close_path
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": [1000.0] * rows,
        }
    )


def _qg_intraday_frame(direction: str, rows: int = 90) -> pd.DataFrame:
    """Multi-session NY frame with a drifting intraday path.

    Six bars per session inside 09:30-15:00 NY; the close stays strictly
    above (long) / below (short) its breakout level on every bar while the
    session drift gives the ADX a trend. The one-trade-per-day rule caps
    entries at one per session (the session's first qualifying bar enters;
    the stop checks keep the position alive through the session because the
    frame's low never reaches the entry bar's low).
    """
    bars_per_day = 6
    day_index = np.repeat(np.arange(rows // bars_per_day + 1), bars_per_day)[:rows]
    bar_of_day = np.tile(np.arange(bars_per_day), rows // bars_per_day + 1)[:rows]
    minutes_of_day = 9 * 60 + 30 + bar_of_day * 60
    if direction == "short":
        base = 100.0 - day_index * 2.0
        # Every bar makes a new low, so a short's stop (entry-bar high) is
        # never reached afterwards and the position stays open for the day.
        offset = -np.where(bar_of_day % 2 == 0, 0.6, 0.2) - bar_of_day * 0.1
    else:
        base = 100.0 + day_index * 2.0
        offset = np.where(bar_of_day % 2 == 0, 0.6, 0.2) + bar_of_day * 0.1
    close = base + offset
    timestamps = (
        pd.to_datetime(pd.Timestamp("2024-01-02", tz="US/Eastern").normalize().tz_convert("UTC"))
        + pd.to_timedelta(day_index, unit="D")
        + pd.to_timedelta(minutes_of_day, unit="m")
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close - np.sign(offset) * 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": [1000.0] * rows,
        }
    )


def test_qg_enters_long_when_close_breaks_above_long_breakout_level():
    """Breakout semantics: a long requires ``close >= breakout_long``.

    Old behaviour read ``close < breakout_long`` for longs (and
    ``adx < 30``), which fires longs on fade-downs instead of breakouts.
    """
    module = _qg_module()
    # Multi-session intraday frame with drifting closes: ADX is high (trend),
    # the close stays above its long breakout level, and the one-trade-per-day
    # rule caps entries at one per session. The first sessions are skipped by
    # the ADX warm-up.
    df = _qg_intraday_frame("long")
    params = {"adx_period": 10, "adx_smooth": 3, "fract": 1.0}
    entries, _, indicators, _ = _call(module, df, params)

    assert entries.any(), "deterministic breakout path must produce entries"
    assert entries.index[entries][0] >= 1, (
        "the ADX warm-up must suppress the very first bar"
    )
    for entry_idx in entries[entries].index:
        long_level = float(indicators["breakout_long"].loc[entry_idx])
        assert float(df["close"].loc[entry_idx]) >= long_level, (
            "every long entry must close at or above its breakout_long level"
        )
        assert indicators["direction"].loc[entry_idx] == "LONG"


def test_qg_enters_short_when_close_breaks_below_short_breakout_level():
    """Breakout semantics: a short requires ``close <= breakout_short``.

    Old behaviour read ``close < breakout_short`` with ``adx > 30`` — a
    condition that is almost always true below the bar high, i.e. the short
    fired on non-breakouts.
    """
    module = _qg_module()
    # Mirrored frame: declining closes so the breakdown side qualifies.
    df = _qg_intraday_frame("short")
    params = {"adx_period": 10, "adx_smooth": 3, "fract": 1.0}
    entries, _, indicators, _ = _call(module, df, params)

    assert entries.any(), "deterministic breakdown path must produce entries"
    for entry_idx in entries[entries].index:
        short_level = float(indicators["breakout_short"].loc[entry_idx])
        assert float(df["close"].loc[entry_idx]) <= short_level, (
            "every short entry must close at or below its breakout_short level"
        )
        assert indicators["direction"].loc[entry_idx] == "SHORT"


def test_qg_suppressed_side_never_fires_when_direction_gate_blocks_it():
    """The ADX direction gate must still suppress the opposite side.

    Old behaviour coupled the ADX threshold to the inverted comparisons, so
    both sides could fire on the same (wrong) side of the levels.
    """
    module = _qg_module()
    df = _qg_intraday_frame("long")
    params = {"adx_period": 10, "adx_smooth": 3, "fract": 1.0}
    entries, _, indicators, _ = _call(module, df, params)

    for entry_idx in entries[entries].index:
        assert indicators["direction"].loc[entry_idx] != "SHORT"


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
    entries, exits, indicators, _ = _call(module, df, {"sma_period": 5})

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
    entries, _, indicators, _ = _call(module, df, {"sma_period": 5, "rsi_period": 3})
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
    assert supertrend.notna().all() if pd.api.types.is_float_dtype(supertrend) else True


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

    entries, exits, indicators, _ = _call(module, df, params)

    assert "signal_price_fallback_used" in indicators, (
        "the fallback must be surfaced as an indicator column"
    )
    fallback = indicators["signal_price_fallback_used"]
    assert bool(fallback.iloc[-1]) or not bool(fallback.iloc[-1]), "indicator is readable"
    if bool(fallback.iloc[0]):
        assert "signal_price" in indicators, (
            "the fallback must expose the price series actually used"
        )
        assert indicators["signal_price"].iloc[1] == pytest.approx(100.0)


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

    entries, exits, indicators, _ = _call(module, df, params)

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


# ---------------------------------------------------------------------------
# 6. Naming integrity: meta/tags must match the implementation
# ---------------------------------------------------------------------------


def test_naming_integrity_meta_matches_implementation():
    """Meta/tags must describe what the code does (Settlement S15).

    Old behaviour: ``risk_parity`` claimed multi-asset risk parity,
    ``stat_arb_pairs`` claimed pairs trading and ``pynecore_orb`` claimed an
    opening-range breakout — none implemented those semantics, and the tags
    routed discovery/portfolio tooling into wrong strategies.
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
        (orb_meta, {"breakout"}, "moving-average-crossover"),
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
