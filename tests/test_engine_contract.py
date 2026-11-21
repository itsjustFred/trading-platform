"""Cross-engine conformance tests.

Documented contract: ``docs/cross-engine-backtest-contract.md``.

These tests pin the engine-neutral facets of the backtest contract that every
engine adapter must satisfy:

- fills (next-bar-open market entry, stop/TP trigger sides),
- end-date boundary (date-only end bound is inclusive of the full day),
- same-bar SL/TP priority (stop-first without intrabar data, chronological
  event with it, flagged fallback when requested intrabar data is missing),
- sizing (risk% from the actual stop distance, capped by max-position /
  max-leverage notionals),
- stats keys (``result.stats`` carries the canonical ``BacktestStats`` set),
- ``total_bars`` (bar count of loaded main-timeframe data — NOT trade count).

Decision Records (Cleanup 16-A, 2026-08-31):
- Same-bar SL/TP priority is stop-first everywhere when intrabar data is
  unavailable. With enabled and available intrabar data, the actual
  chronological event is used; if intrabar data is missing, fall back to
  stop-first and flag the result.
- A date-only end boundary is inclusive of the full day.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.contract import (
    EngineStatsKeys,
    apply_date_filter,
    flag_missing_intrabar,
    normalize_ohlcv_frame,
    parse_date_bound,
    resolve_intrabar_timeframe,
    resolve_same_bar_exit,
)
from runner.engines.native_adapter import NativeAdapter
from runner.engines.pynecore_adapter import (
    PyneCoreAdapter,
    _pyne_cli_end_bound,
    _pyne_cli_start_bound,
)
from runner.engines.pynecore_adapter import (
    _parse_stats_csv as _real_parse_stats_csv,
)
from runner.engines.pynecore_adapter import (
    _parse_trade_csv as _parse_pyne_trade_csv,
)
from runner.engines.signal_replay_adapter import (
    ReplaySignal,
    SignalReplayAdapter,
    SignalReplayEngine,
)
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.stats import compute_stats
from shared.models import VALID_TIMEFRAMES, Trade, TradeExit
from tests.helpers import with_zero_fees

# ─── Shared fixture builders ─────────────────────────────────────────────────

STRATEGY_PATH = str(Path(__file__).resolve().parent / "strategies" / "contract_sma.py")


def _deterministic_ohlcv(
    *,
    periods: int = 260,
    freq: str = "h",
    start: str = "2024-01-01",
) -> pd.DataFrame:
    """A fully deterministic trend/consolidation series (no random component).

    Alternating 40-bar trends (up +0.5/bar, down -0.5/bar) with 40-bar flat
    phases in between; with SMA(5)/SMA(20) this produces several crossover
    round-trips — entries on the trend starts, exits on the trend ends.
    """
    closes: list[float] = []
    price = 100.0
    # up, flat, down, flat, ... (periods / 80 cycles). Flat phases sit strictly
    # between the trend legs so SMA(5)/SMA(20) fully re-converges and the next
    # trend leg produces a fresh crossover.
    for _ in range(max(1, periods // 80)):
        for _ in range(30):
            price += 0.5
            closes.append(price)
        closes.extend(price for _ in range(20))
        for _ in range(30):
            price -= 0.5
            closes.append(price)
        closes.extend(price for _ in range(20))
    closes = closes[:periods]
    timestamps = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.25 for c in closes],
            "low": [c - 0.25 for c in closes],
            "close": closes,
            "volume": [1000.0] * periods,
        }
    )


def _write_ohlcv(data_dir: Path, symbol: str, timeframe: str, frame: pd.DataFrame) -> Path:
    symbol_dir = data_dir / "ohlcv" / "test_source" / "equities" / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    path = symbol_dir / f"{timeframe}.parquet"
    frame.to_parquet(path, index=False)
    return path


def _config(data_dir: Path, **overrides) -> BacktestConfig:
    payload = {
        "strategy_name": "example_sma",
        "symbols": ["PARITY"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000,
        # C1: engines size zero times without explicit sizing — fixtures
        # declare the standard risk budget (overridable per test).
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(data_dir),
        "parameters": {"fast_length": 5, "slow_length": 20},
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


@pytest.fixture(scope="module")
def contract_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One deterministic bundled fixture tree shared by the engine-level tests."""
    data_dir = tmp_path_factory.mktemp("contract-data")
    _write_ohlcv(data_dir, "PARITY", "1h", _deterministic_ohlcv())
    return data_dir


# ─── Contract document is present and pins the documented facets ────────────


def test_contract_document_exists_and_covers_required_facets():
    document = (
        Path(__file__).resolve().parents[1] / "docs" / "cross-engine-backtest-contract.md"
    )
    assert document.exists(), "The cross-engine contract must be documented"

    text = document.read_text(encoding="utf-8")
    for facet in (
        "fills",
        "end-date boundary",
        "same-bar",
        "sizing",
        "stats keys",
        "total_bars",
        "execution-timing",
    ):
        assert facet in text.lower(), f"Contract document must cover '{facet}'"


# ─── Date filter convention (F9) ─────────────────────────────────────────────


class TestDateFilterConvention:
    """Decision Record: a date-only end boundary is inclusive of the full day."""

    def test_date_only_end_bound_includes_full_day(self):
        bound = parse_date_bound("2024-01-15", is_end=True)
        assert bound == pd.Timestamp("2024-01-15 23:59:59.999999", tz="UTC")

    def test_date_only_start_bound_is_start_of_day(self):
        bound = parse_date_bound("2024-01-15", is_end=False)
        assert bound == pd.Timestamp("2024-01-15 00:00:00", tz="UTC")

    def test_explicit_time_bound_is_used_as_is(self):
        bound = parse_date_bound("2024-01-15T14:30:00Z", is_end=True)
        assert bound == pd.Timestamp("2024-01-15 14:30:00", tz="UTC")

    def test_naive_timestamp_is_treated_as_utc(self):
        bound = parse_date_bound("2024-01-15T14:30:00", is_end=False)
        assert bound == pd.Timestamp("2024-01-15 14:30:00", tz="UTC")

    def test_empty_and_invalid_values_are_rejected(self):
        with pytest.raises(ValueError):
            parse_date_bound("", is_end=True)
        with pytest.raises(ValueError):
            parse_date_bound("not-a-date", is_end=True)

    def test_last_bar_of_end_day_survives_the_filter(self):
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2024-01-14 23:00", "2024-01-15 22:00", "2024-01-16 02:00"], utc=True
                )
            }
        )
        filtered = apply_date_filter(frame, start="2024-01-01", end="2024-01-15")
        assert len(filtered) == 2, "Bars on the end date itself must be included"
        assert filtered["timestamp"].max() == pd.Timestamp("2024-01-15 22:00", tz="UTC")

    def test_engines_agree_on_the_bundled_fixture_window(self, contract_data_dir):
        """Conformance: all engines cut data with the same inclusive end bound.

        The bundled fixture runs hourly from 2024-01-01; every engine run with
        end_date="2024-01-09" must therefore load exactly the same bar set
        (through 2024-01-09 23:59:59.999999 = 216 bars) and agree on
        total_bars.
        """
        data_dir = tmp_contract_dir(contract_data_dir)
        cfg = _config(data_dir, end_date="2024-01-09", parameters={})

        native = NativeAdapter().run(cfg, STRATEGY_PATH)
        vectorbt = VectorbtAdapter().run(cfg, STRATEGY_PATH)

        # Bars from 2024-01-01 00:00 through 2024-01-09 23:00 inclusive.
        for result in (native, vectorbt):
            assert result.meta.total_bars == 216, (
                f"{result.meta.engine}: end-date boundary kept "
                f"{result.meta.total_bars} bars, expected the full-day-inclusive window (216)"
            )

    def test_signal_replay_loader_keeps_the_full_end_day(self, tmp_path: Path):
        """Conformance (F9): signal-replay cuts data with the same convention.

        The replay adapter loads its own OHLCV from disk; a bare-date
        comparison there would truncate at midnight and silently drop the
        whole end day, diverging from the other engines.
        """
        frame = _deterministic_ohlcv(periods=240, freq="h")  # 2024-01-01 .. 2024-01-10 23:00
        _write_ohlcv(tmp_path, "PARITY", "1h", frame)

        adapter = SignalReplayEngine(data_dir=tmp_path)
        loaded = adapter._load_ohlcv(
            symbol="PARITY", timeframe="1h", start="2024-01-01", end="2024-01-05"
        )

        assert loaded is not None
        assert loaded["timestamp"].max() == pd.Timestamp("2024-01-05 23:00", tz="UTC"), (
            "A date-only end bound must keep the bars of the end date itself"
        )
        assert loaded["timestamp"].min() == pd.Timestamp("2024-01-01 00:00", tz="UTC")


def tmp_contract_dir(shared_dir: Path) -> Path:
    """The module fixture already carries the PARITY/1h frame."""
    return shared_dir


# ─── Same-bar SL/TP priority (F8) ────────────────────────────────────────────


class TestSameBarExitPolicy:
    """Decision Record: stop-first when intrabar data is unavailable."""

    def test_stop_wins_when_both_levels_hit_same_bar(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=112.0, low=109.0, stop_price=109.5, take_profit_price=111.5
            )
            == "stop"
        )

    def test_tp_wins_when_only_tp_is_hit(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=112.0, low=111.0, stop_price=109.0, take_profit_price=111.5
            )
            == "tp"
        )

    def test_stop_wins_when_only_stop_is_hit(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=110.0, low=108.0, stop_price=109.0, take_profit_price=111.0
            )
            == "stop"
        )

    def test_short_side_mirrors_the_rule(self):
        assert (
            resolve_same_bar_exit(
                direction="SHORT", high=111.0, low=108.0, stop_price=110.5, take_profit_price=108.5
            )
            == "stop"
        )
        assert (
            resolve_same_bar_exit(
                direction="SHORT", high=109.0, low=107.0, stop_price=110.5, take_profit_price=108.5
            )
            == "tp"
        )

    def test_no_hit_returns_none(self):
        assert (
            resolve_same_bar_exit(
                direction="LONG", high=110.0, low=109.5, stop_price=108.0, take_profit_price=112.0
            )
            is None
        )

    def test_unknown_direction_is_rejected_not_treated_as_short(self):
        """A direction spelling outside the engine convention must fail loudly.

        The helper's LONG/SHORT branch would otherwise silently evaluate an
        unknown value as SHORT and invert stop/TP logic into plausible-looking
        wrong exits.
        """
        with pytest.raises(ValueError):
            resolve_same_bar_exit(
                direction="long", high=110.0, low=109.5, stop_price=108.0, take_profit_price=112.0
            )
        with pytest.raises(ValueError):
            resolve_same_bar_exit(
                direction="", high=110.0, low=109.5, stop_price=108.0, take_profit_price=112.0
            )

    def test_missing_intrabar_data_is_flagged(self):
        flag = flag_missing_intrabar(
            intrabar_resolution="1-lower",
            intrabar_coverage=False,
            reason="intrabar_data_missing",
        )
        assert flag is not None
        assert flag["same_bar_priority"] == "stop_first_fallback"
        assert flag["intrabar_available"] is False
        assert flag["reason"] == "intrabar_data_missing"

        empty = flag_missing_intrabar(
            intrabar_resolution="5m",
            intrabar_coverage=False,
            reason="intrabar_data_missing",
        )
        assert empty is not None
        assert empty["intrabar_available"] is False

    def test_sub_bars_present_but_unresolvable_is_flagged_as_unresolved(self):
        """Sub-bars exist but the walk found no ordered event: still a fallback.

        Coverage alone is not resolution — the flag must be emitted (so the
        stop-first fallback stays visible) and must record that the data was
        there but could not order the two levels.
        """
        flag = flag_missing_intrabar(
            intrabar_resolution="5m", intrabar_coverage=True, reason="intrabar_unresolved"
        )
        assert flag is not None
        assert flag["same_bar_priority"] == "stop_first_fallback"
        assert flag["intrabar_available"] is True
        assert flag["reason"] == "intrabar_unresolved"

    def test_resolved_walk_does_not_reach_the_helper(self):
        """A RESOLVED walk returns its event directly — the helper is never called.

        The helper only ever emits or raises; there is no argument combination
        that silently suppresses the flag (the earlier default-reason shortcut
        made ``coverage=True`` without an explicit reason read as "resolved"
        and dropped the flag). The engine's call site proves the resolved path
        returns before reaching the fallback:
        ``_check_stops`` → ``_intrabar_first_trigger`` returning an event skips
        ``_flag_same_bar_fallback`` entirely.
        """
        # Both call sites in the engine pass an explicit reason; a resolved
        # walk short-circuits before either is reachable.
        resolved_event = resolve_same_bar_exit(
            direction="LONG", high=112.5, low=104.0, stop_price=100.0, take_profit_price=110.0
        )
        assert resolved_event == "tp"

    def test_no_intrabar_resolution_requested_is_not_flagged(self):
        assert flag_missing_intrabar(
            intrabar_resolution=None,
            intrabar_coverage=False,
            reason="intrabar_data_missing",
        ) is None

    def test_reason_is_required_not_defaulted(self):
        """The failure mode must be stated explicitly by every caller.

        A defaulted ``reason`` let ``intrabar_coverage=True`` without a reason
        silently suppress the flag — the exact unflagged fallback the Decision
        Record requires to be visible.
        """
        with pytest.raises(TypeError):
            flag_missing_intrabar(intrabar_resolution="5m", intrabar_coverage=True)  # type: ignore[call-arg]

    def test_unknown_reason_is_rejected(self):
        with pytest.raises(ValueError):
            flag_missing_intrabar(
                intrabar_resolution="5m",
                intrabar_coverage=True,
                reason="made_up_reason",
            )


def _sl_tp_strategy(path: Path, *, stop: float, take_profit: float, entry: int = 2) -> None:
    """Entry on bar ``entry``; exit on the last bar (both engines shift)."""
    path.write_text(
        f"""
import pandas as pd

def generate_signals(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[{entry}] = True
    exits.iloc[len(df) - 1] = True
    indicators = {{
        "direction": pd.Series("LONG", index=df.index),
        "stop_loss": pd.Series(pd.NA, index=df.index),
        "take_profit": pd.Series(pd.NA, index=df.index),
    }}
    indicators["stop_loss"].iloc[{entry}] = {stop!r}
    indicators["take_profit"].iloc[{entry}] = {take_profit!r}
    return entries, exits, indicators
"""
    )


def test_native_engine_stops_first_without_intrabar_data(tmp_path: Path):
    """Conformance (F8): no intrabar data -> both levels hit -> stop wins."""
    data = _deterministic_ohlcv(periods=10, freq="h")
    # Bar 5 trades through BOTH the stop and the take-profit level.
    data.loc[5, "high"] = 114.0
    data.loc[5, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        parameters={},
    )
    result = NativeAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "stop_loss", (
        "Without intrabar data the stop must win the same-bar ambiguity"
    )
    assert trade.exits[-1].price == pytest.approx(100.0)


def test_vectorbt_engine_stops_first_without_intrabar_data(tmp_path: Path):
    """Conformance (F8): vectorbt's intrabar path is off -> stop-first too.

    vectorbt resolves protective levels on the entry bar and only consults them
    through the intrabar walk; with no intrabar data the trade exits on the
    scheduled signal exit and the levels are recorded as meta. Conformance
    here means: the recorded stop level is the strategy's stop (the
    stop-first-fallback contract's input), and NO exit leg may claim a
    take-profit fill that the stop-first fallback denies.
    """
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[5, "high"] = 114.0
    data.loc[5, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        parameters={},
    )
    result = VectorbtAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.meta.get("sl_price") == pytest.approx(100.0)
    assert trade.meta.get("tp_price") == pytest.approx(110.0)
    assert trade.exits[-1].reason != "take_profit", (
        "Without intrabar data no exit may claim the take-profit the "
        "stop-first fallback denies"
    )


INTRABAR_SUBBARS = {
    # 5m sub-bars for the 04:00 hour (both engines' intrabar window): the
    # price rallies through the take-profit first, then dips through the
    # stop. The 1h main frame trades through BOTH levels on bar 4, so the
    # stop-first tie-break would report stop_loss; the chronological event
    # is the take-profit.
    "timestamp": pd.date_range("2024-01-01 04:00", periods=12, freq="5min", tz="UTC"),
    "open": [104.0, 105.0, 106.0, 108.0, 110.0, 112.0, 110.0, 108.0, 105.0, 100.6, 100.4, 100.5],
    "high": [105.5, 106.0, 108.5, 110.5, 112.5, 114.0, 110.8, 108.9, 106.0, 100.9, 100.8, 104.5],
    "low": [104.4, 105.0, 106.0, 108.0, 110.0, 111.0, 108.0, 105.0, 96.0, 100.0, 96.0, 100.4],
    "close": [105.0, 106.0, 108.0, 110.0, 112.0, 113.0, 110.0, 108.0, 96.5, 100.4, 100.5, 100.6],
    "volume": [100.0] * 12,
}


def _write_intrabar_fixture(tmp_path: Path) -> pd.DataFrame:
    """Main frame trades through both levels on bar 4; sub-bars order the events.

    Bar 4 follows the entry bar (entry fills at bar 3's open), so the
    ambiguous bar is seen while the position is open in both engines.
    """
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[4, "high"] = 114.0
    data.loc[4, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)
    intrabar = pd.DataFrame(INTRABAR_SUBBARS)
    _write_ohlcv(tmp_path, "PARITY", "5m", intrabar)
    return data


def test_vectorbt_engine_uses_chronological_intrabar_event_when_available(tmp_path: Path):
    """Conformance (F8): intrabar enabled+available -> the actual event wins.

    Within the ambiguous hour the price rallies through the take-profit
    (per the 5m sub-bars) before dipping through the stop. Stop-first would
    report stop_loss; the chronological intrabar walk must report
    take_profit.
    """
    _write_intrabar_fixture(tmp_path)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="5m",
        parameters={},
    )
    result = VectorbtAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "take_profit", (
        "With intrabar data available the chronological event must win"
    )
    assert trade.exits[-1].price == pytest.approx(110.0)
    assert trade.meta.get("modeling_resolution") == "5m"


def test_native_engine_uses_chronological_intrabar_event_when_available(tmp_path: Path):
    """Conformance (F8): native engine, same scenario, chronological event wins."""
    _write_intrabar_fixture(tmp_path)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="5m",
        parameters={},
    )
    result = NativeAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    # The native engine names TP exits after the TP level ("tp"); vectorbt
    # uses "take_profit". Either way the CHRONOLOGICAL event must win over
    # the stop-first tie-break.
    assert trade.exits[-1].reason in {"tp", "take_profit"}, (
        f"With intrabar data available the chronological event must win, "
        f"got {trade.exits[-1].reason!r}"
    )
    assert trade.exits[-1].price == pytest.approx(110.0)


def test_native_engine_flags_the_stop_first_fallback_when_intrabar_is_missing(tmp_path: Path):
    """Decision Record: intrabar requested but missing -> stop-first AND flag."""
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[4, "high"] = 114.0
    data.loc[4, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="5m",
        parameters={},
    )
    result = NativeAdapter().run(cfg, str(strategy))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[-1].reason == "stop_loss"
    flag = trade.meta.get("same_bar_priority")
    assert flag is not None, "Missing intrabar data must flag the stop-first fallback"
    assert flag["same_bar_priority"] == "stop_first_fallback"
    assert flag["intrabar_available"] is False


def test_native_engine_resolves_one_lower_like_vectorbt(tmp_path: Path):
    """Conformance (F8 parity): ``modeling_resolution="1-lower"`` means the same
    thing in every engine.

    vectorbt resolves ``1-lower`` on a 1h main timeframe to the 30m intrabar
    frame (:func:`_resolve_intrabar_timeframe`); the native engine must resolve
    it to the same granularity. Otherwise the same configuration produces a
    stop-first fallback on one engine and a chronological exit on the other —
    a parity divergence for identical input.
    """
    # The 1h main frame trades through BOTH levels on bar 4; the 30m sub-bars
    # order the events (take-profit first) exactly like the 5m fixture above,
    # scaled to 30-minute steps.
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[4, "high"] = 114.0
    data.loc[4, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    intrabar = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01 04:00", periods=2, freq="30min", tz="UTC"),
            # 04:00 sub-bar rallies through the TP (110.0) without touching the
            # stop (100.0); the 04:30 sub-bar trades through both.
            "open": [104.0, 100.6],
            "high": [112.5, 104.5],
            "low": [104.0, 96.0],
            "close": [112.0, 100.5],
            "volume": [100.0, 100.0],
        }
    )
    _write_ohlcv(tmp_path, "PARITY", "30m", intrabar)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    cfg = _config(
        tmp_path,
        strategy_name="sl_tp_strategy",
        start_date="2024-01-01",
        end_date="2024-01-01",
        modeling_resolution="1-lower",
        parameters={},
    )

    native = NativeAdapter().run(cfg, str(strategy))
    assert len(native.trades) == 1
    native_trade = native.trades[0]
    assert native_trade.exits[-1].reason in {"tp", "take_profit"}, (
        "native must resolve '1-lower' to the 30m intrabar frame like vectorbt "
        "does; a stop-first exit means the intrabar frame was never loaded"
    )
    assert native_trade.exits[-1].price == pytest.approx(110.0)
    flag = native_trade.meta.get("same_bar_priority")
    assert flag is None, "with the intrabar frame available no fallback flag may be recorded"

    vectorbt = VectorbtAdapter().run(cfg, str(strategy))
    assert len(vectorbt.trades) == 1
    assert vectorbt.trades[0].exits[-1].reason == "take_profit", (
        "vectorbt already resolves '1-lower' chronologically; native must agree"
    )


def test_engines_agree_on_unresolvable_intrabar_spellings(tmp_path: Path):
    """Conformance: spellings no engine can resolve degrade identically.

    ``main_timeframe`` (and the open/close default) request no intrabar walk in
    any engine; the stop-first fallback with no flag is the documented behavior
    everywhere.
    """
    data = _deterministic_ohlcv(periods=10, freq="h")
    data.loc[4, "high"] = 114.0
    data.loc[4, "low"] = 96.0
    _write_ohlcv(tmp_path, "PARITY", "1h", data)

    strategy = tmp_path / "sl_tp_strategy.py"
    _sl_tp_strategy(strategy, stop=100.0, take_profit=110.0)

    for resolution in ("main_timeframe", "open_close"):
        cfg = _config(
            tmp_path,
            strategy_name="sl_tp_strategy",
            start_date="2024-01-01",
            end_date="2024-01-01",
            modeling_resolution=resolution,
            parameters={},
        )
        native_trade = NativeAdapter().run(cfg, str(strategy)).trades[0]
        assert native_trade.exits[-1].reason == "stop_loss"
        assert native_trade.meta.get("same_bar_priority") is None, (
            f"{resolution}: no intrabar resolution was requested, so the "
            "stop-first fallback is the documented default and is not flagged"
        )


def test_native_and_vectorbt_resolve_the_same_intrabar_spellings(tmp_path: Path):
    """Conformance: native and vectorbt resolve every spelling identically.

    Both adapters must agree for EVERY ``modeling_resolution`` spelling the
    config validator accepts — including ``m1`` (an alias for ``1m`` that only
    vectorbt normalized; native fell through to no-intrabar, diverging from
    the documented "same spellings, same way"). The two engines must resolve
    each spelling to the same intrabar timeframe (or both to none).
    """
    from runner.engines.vectorbt_adapter import _resolve_intrabar_timeframe

    resolutions = [
        "main_timeframe", "open_close", "1-lower", "m1",
        *VALID_TIMEFRAMES,
    ]
    for resolution in resolutions:
        for base in ("1h", "1d"):
            cfg = BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero"},
                strategy_name="parity",
                symbols=["X"],
                timeframe=base,
                start_date="2024-01-01",
                end_date="2024-01-02",
                modeling_resolution=resolution,
            )
            # The config validator canonicalizes aliases; both engines then
            # resolve the SAME canonical spelling through the SAME shared
            # resolver, so parity is asserted at the resolver level.
            native_tf = resolve_intrabar_timeframe(base, cfg.modeling_resolution)
            vectorbt_tf = _resolve_intrabar_timeframe(base, cfg.modeling_resolution)
            assert (native_tf or None) == (vectorbt_tf or None), (
                f"modeling_resolution={resolution!r} on {base}: native resolves "
                f"{native_tf!r} but vectorbt resolves {vectorbt_tf!r} — "
                "the spellings must resolve identically in every engine"
            )

    # The m1 alias must actually resolve to the 1m frame (the divergence the
    # reviewer probed: native None, vectorbt 1m).
    assert resolve_intrabar_timeframe("1h", "m1") == "1m"
    assert resolve_intrabar_timeframe("1h", "M1") == "1m"


def test_spellings_pinned_against_one_shared_table():
    """The resolution convention lives in exactly one place.

    Both engines must go through the SAME spelling table
    (``contract.normalize_intrabar_resolution``); a private per-adapter copy
    would let the two conventions drift apart again (exactly the ``m1``
    divergence this test family pins).
    """
    import runner.engines.native_adapter as native_module
    import runner.engines.vectorbt_adapter as vbt_module
    from runner.engines import contract

    # The shared table exists and behaves per the documented convention.
    assert contract.normalize_intrabar_resolution("m1") == "1m"
    assert contract.normalize_intrabar_resolution("1-lower") == "1-lower"
    assert contract.normalize_intrabar_resolution("main_timeframe") == "open_close"
    assert contract.normalize_intrabar_resolution("open_close") == "open_close"
    assert contract.normalize_intrabar_resolution("unknown-spelling") == "open_close"
    # Both adapters expose the SAME table object (no private copies).
    assert native_module.ONE_LOWER_SPELLINGS is contract.ONE_LOWER_SPELLINGS
    assert native_module.TIMEFRAME_ORDER is contract.TIMEFRAME_ORDER
    # The vectorbt adapter is the one that used to keep a private literal copy of
    # TIMEFRAME_ORDER, and this test imported it without ever asserting on it, so
    # the divergence was invisible. Identity, not equality: a private copy with
    # the same values would pass an == check and still drift later.
    assert vbt_module.TIMEFRAME_ORDER is contract.TIMEFRAME_ORDER
    assert vbt_module._normalize_intrabar_resolution("m1") == contract.normalize_intrabar_resolution("m1")
    assert vbt_module._resolve_intrabar_timeframe("1h", "m1") == contract.resolve_intrabar_timeframe("1h", "m1")
    # The config validator canonicalizes alias spellings before any adapter
    # sees them, so per-adapter divergence is structurally impossible.
    cfg = BacktestConfig(risk_per_trade_pct=0.01, parameters={"fees": "zero"},
        strategy_name="pin",
        symbols=["X"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-01-02",
        modeling_resolution="M1",
    )
    assert cfg.modeling_resolution == "1m"


# ─── Sizing (F3) ─────────────────────────────────────────────────────────────


class TestSizingContract:
    """Conformance: risk% sizing derives quantity from the actual stop distance.

    risk_target = initial_capital * risk_per_trade_pct; the stop distance is
    |entry - stop|; qty = risk_target / stop_distance, capped by the
    max-position and max-leverage notionals.
    """

    def test_native_engine_sizes_from_actual_stop(self):
        engine_cfg = BacktestConfig(
            strategy_name="unit-test",
            symbols=["TEST"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            parameters={"fees": "zero"},
        )
        from runner.engine import TradeEngine

        engine = TradeEngine(engine_cfg, symbol="TEST")
        quantity = engine._calculate_position_size(
            entry_price=100.0, stop_loss_price=95.0, direction="LONG"
        )
        # 1% of 100k = 1000 USD at risk over a 5-point stop => 200 units.
        assert quantity == pytest.approx(200.0)

    def test_vectorbt_engine_sizes_from_actual_stop(self, tmp_path: Path):
        data = _deterministic_ohlcv(periods=10, freq="h")
        _write_ohlcv(tmp_path, "PARITY", "1h", data)

        strategy = tmp_path / "sl_tp_strategy.py"
        _sl_tp_strategy(strategy, stop=95.0, take_profit=200.0)

        cfg = _config(
            tmp_path,
            strategy_name="sl_tp_strategy",
            start_date="2024-01-01",
            end_date="2024-01-01",
            initial_capital=100_000,
            risk_per_trade_pct=0.01,
            parameters={},
        )
        result = VectorbtAdapter().run(cfg, str(strategy))

        assert len(result.trades) == 1
        trade = result.trades[0]
        entry_price = trade.entry_price
        stop_distance = entry_price - 95.0
        expected_qty = (100_000 * 0.01) / stop_distance
        assert trade.quantity == pytest.approx(expected_qty, rel=1e-3)

    def test_vectorbt_engine_caps_notional_at_max_leverage(self, tmp_path: Path):
        data = _deterministic_ohlcv(periods=10, freq="h")
        _write_ohlcv(tmp_path, "PARITY", "1h", data)

        strategy = tmp_path / "sl_tp_strategy.py"
        # P3a: stop must sit on the valid side of the ~102.0 entry — a
        # wrong-side stop (e.g. the previous 103.9) is correctly dropped by
        # the inverted-stop guard, which would void this leverage-cap test.
        _sl_tp_strategy(strategy, stop=100.0, take_profit=200.0)

        cfg = _config(
            tmp_path,
            strategy_name="sl_tp_strategy",
            start_date="2024-01-01",
            end_date="2024-01-01",
            initial_capital=100_000,
            risk_per_trade_pct=0.5,  # absurd risk -> must hit the leverage cap
            max_leverage=1.0,
            max_position_pct=1.0,
            parameters={},
        )
        result = VectorbtAdapter().run(cfg, str(strategy))

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.meta["risk_math"]["capped_by_max_leverage"] is True
        assert trade.meta["risk_math"]["final_notional"] == pytest.approx(100_000, rel=1e-6)


# ─── Stats keys (F5) ─────────────────────────────────────────────────────────


class TestStatsKeysContract:
    """Conformance: every engine can expose the canonical stats key set."""

    REQUIRED_KEYS = sorted(EngineStatsKeys.REQUIRED)

    # Stubbed ``pyne`` CLI outputs (the subprocess boundary is the one seam the
    # suite may not cross); everything after it in the adapter runs for real.
    PYNE_TRADE_CSV = (
        "Trade #,Bar Index,Type,Signal,Date/Time,Price USD,Contracts,"
        "Profit USD,Profit %,Cumulative profit USD,Cumulative profit %,"
        "Run-up USD,Run-up %,Drawdown USD,Drawdown %\n"
        "1,47,Entry long,Long,2024-01-05T10:00:00+0000,104.0,1.0,0.0,0.00,0.0,"
        "0.00,0.0,0.0,0.0,0.0\n"
        "1,72,Exit long,Close entry(s) order Long,2024-01-08T15:00:00+0000,106.5,"
        "1.0,2.5,0.01,2.5,0.01,3.0,0.02,0.1,0.0\n"
    )
    PYNE_STATS_CSV = (
        "Metric,All USD,All %,Long USD,Long %,Short USD,Short %\n"
        "Net profit,2.5,0.01,2.5,0.01,0.0,0.0\n"
        "Total Closed Trades,1,1,1,1,0,0\n"
    )

    def test_contract_key_set_matches_backteststats(self):
        from shared.models import BacktestStats

        canonical = BacktestStats().to_dict()
        missing = EngineStatsKeys.missing_from(canonical)
        assert not missing, f"Contract keys must be a subset of BacktestStats: {missing}"

    def test_optional_key_set_matches_backteststats_optional_fields(self):
        """The OPTIONAL family mirrors BacktestStats's optional fields exactly.

        Pinning the two sets against each other keeps ``OPTIONAL`` from
        rotating independently: a key added here without a ``BacktestStats``
        field (or vice versa) is a contract change that must be made
        deliberately in both places, not a silent drift.
        """
        from dataclasses import fields

        from shared.models import BacktestStats

        # Fields of BacktestStats that are NOT part of the required contract
        # key set are exactly the optional family.
        required_and_meta = EngineStatsKeys.REQUIRED | {"cagr", "extras"}
        model_optional = {
            field.name
            for field in fields(BacktestStats)
            if field.name not in required_and_meta
        }
        assert model_optional == set(EngineStatsKeys.OPTIONAL), (
            f"EngineStatsKeys.OPTIONAL {sorted(EngineStatsKeys.OPTIONAL)} drifted from "
            f"BacktestStats's optional fields {sorted(model_optional)} — update both "
            "together (see contract.py: OPTIONAL is not an extension point)"
        )

    def test_signal_replay_result_carries_the_canonical_stats_keys(self):
        frame = _deterministic_ohlcv(periods=40, freq="h")
        signals = [
            {
                "timestamp": "2024-01-01T02:00:00Z",
                "symbol": "PARITY",
                "action": "buy",
                "entry_price": 101.5,
                "stop_loss": 95.0,
                "take_profit": 130.0,
            },
            {
                "timestamp": "2024-01-01T20:00:00Z",
                "symbol": "PARITY",
                "action": "sell",
                "entry_price": 108.0,
            },
        ]
        result = SignalReplayAdapter(fee_params={"fees": "zero"}, ).run(
            signals=[ReplaySignal.from_payload(signal) for signal in signals],
            ohlcv_by_symbol={"PARITY": frame},
            timeframe="1h",
        )
        missing = EngineStatsKeys.missing_from(result.stats)
        assert not missing, f"signal-replay stats are missing canonical keys: {sorted(missing)}"

    def test_native_engine_result_can_expose_the_canonical_stats_keys(
        self, contract_data_dir: Path
    ):
        result = NativeAdapter().run(_config(tmp_contract_dir(contract_data_dir)), STRATEGY_PATH)
        assert len(result.trades) > 0

        stats = compute_stats(
            trades=result.trades,
            initial_capital=result.meta.initial_capital,
            equity_curve=result.equity_curve or None,
        )
        missing = EngineStatsKeys.missing_from(stats)
        assert not missing, f"native stats are missing canonical keys: {sorted(missing)}"

    def test_vectorbt_engine_result_can_expose_the_canonical_stats_keys(
        self, contract_data_dir: Path
    ):
        result = VectorbtAdapter().run(
            _config(tmp_contract_dir(contract_data_dir)), STRATEGY_PATH
        )
        assert len(result.trades) > 0

        stats = compute_stats(
            trades=result.trades,
            initial_capital=result.meta.initial_capital,
            equity_curve=result.equity_curve or None,
        )
        missing = EngineStatsKeys.missing_from(stats)
        assert not missing, f"vectorbt stats are missing canonical keys: {sorted(missing)}"

    def test_pynecore_result_can_expose_the_canonical_stats_keys(self, contract_data_dir: Path):
        """Conformance: pynecore's stats dict must carry the canonical keys.

        Unlike the previous revision of this test (which ran NativeAdapter and
        called the merge helper itself — exercising no PyneCore code at all),
        this drives the actual PyneCoreAdapter.run() path: strategy artifact
        loading, the CLI dispatch (stubbed at the subprocess boundary — the
        adapter-internal code under test is real), CSV trade parsing,
        restatement containment, risk sizing, equity derivation, and the
        canonical-stats merge the contract requires.
        """
        data_dir = tmp_contract_dir(contract_data_dir)
        cfg = _config(data_dir, parameters={})

        result = _run_pyne_with_stubbed_cli(
            cfg, STRATEGY_PATH,
            trade_csv_text=self.PYNE_TRADE_CSV, stats_csv_text=self.PYNE_STATS_CSV,
        )

        assert len(result.trades) > 0, "the stubbed CLI output must yield trades"
        missing = EngineStatsKeys.missing_from(result.stats)
        assert not missing, f"pynecore stats are missing canonical keys: {sorted(missing)}"
        # Engine-specific strategy stats survive the merge (they may add keys
        # but never remove the canonical ones — contract §6).
        assert any(k.startswith("net_profit") or k == "strategy_net_profit" for k in result.stats), (
            "the pynecore strategy stats must be merged on top of the canonical set"
        )

    def test_engine_specific_stats_may_not_shadow_canonical_keys(self, contract_data_dir: Path):
        """Contract §6: engine stats are merged ON TOP of the canonical ones.

        The merge direction is canonical-basis + engine-overlay, and an engine
        stat that happens to share a name with a canonical key must never
        replace the canonical value — consumers (viewer, walk-forward, live
        scoring) read ``result.stats`` uniformly and must not see a
        bespoke/derived value silently take over a canonical one.
        """
        data_dir = tmp_contract_dir(contract_data_dir)
        cfg = _config(data_dir, parameters={})

        # "net_profit" is a pynecore strategy stat; pretend the parsed strategy
        # stats also carry a canonical key ("total_pnl") with a bogus value to
        # prove the merge keeps the canonical one underneath.
        with patch(
            "runner.engines.pynecore_adapter._parse_stats_csv",
            side_effect=lambda path: {**_real_parse_stats_csv(path), "total_pnl": 99.0},
        ):
            result = _run_pyne_with_stubbed_cli(
                cfg, STRATEGY_PATH,
                trade_csv_text=self.PYNE_TRADE_CSV, stats_csv_text=self.PYNE_STATS_CSV,
            )

        assert result.stats.get("total_pnl") != 99.0, (
            "the engine-specific strategy stat must not shadow the canonical "
            "total_pnl (contract §6: engine stats may add keys, never remove them)"
        )
        assert "net_profit" in result.stats, (
            "engine-specific keys that do not collide are still merged on top"
        )


# ─── total_bars (F6) ─────────────────────────────────────────────────────────


class TestTotalBarsContract:
    """Conformance: ``meta.total_bars`` = loaded main-timeframe bar count.

    F6: the vectorbt adapter historically persisted the trade count here.
    Every engine must report the number of bars of market data actually loaded
    for the backtest, summed across symbols.
    """

    def test_native_engine_reports_loaded_bar_count(self, contract_data_dir: Path):
        data_dir = tmp_contract_dir(contract_data_dir)
        frame = _read_parquet(data_dir, "PARITY", "1h")
        result = NativeAdapter().run(_config(data_dir), STRATEGY_PATH)
        assert result.meta.total_bars == len(frame)

    def test_vectorbt_engine_reports_loaded_bar_count(self, contract_data_dir: Path):
        data_dir = tmp_contract_dir(contract_data_dir)
        frame = _read_parquet(data_dir, "PARITY", "1h")
        result = VectorbtAdapter().run(_config(data_dir), STRATEGY_PATH)
        assert len(result.trades) > 0, "Fixture must trade for the assertion to bite"
        assert result.meta.total_bars == len(frame), (
            "total_bars must be the loaded bar count, not the trade count (F6)"
        )

    def test_pynecore_engine_reports_loaded_bar_count(self, contract_data_dir: Path):
        """Conformance: pynecore sums ``len(df)`` over the loaded symbols."""
        data_dir = tmp_contract_dir(contract_data_dir)
        frame = _read_parquet(data_dir, "PARITY", "1h")
        # The pyne CLI is stubbed at the subprocess boundary (as in the other
        # pyne tests): F11 makes a genuinely missing binary a loud failure, so
        # the bar-count contract must be observable with the CLI dispatch
        # stubbed — total_bars reflects the bars loaded (not trades, not zero).
        result = _run_pyne_with_stubbed_cli(
            _config(data_dir), STRATEGY_PATH,
            trade_csv_text=TestStatsKeysContract.PYNE_TRADE_CSV,
            stats_csv_text=TestStatsKeysContract.PYNE_STATS_CSV,
        )
        assert result.meta.total_bars == len(frame)

    def test_total_bars_is_not_the_trade_count(self, contract_data_dir: Path):
        data_dir = tmp_contract_dir(contract_data_dir)
        for adapter in (NativeAdapter(), VectorbtAdapter()):
            result = adapter.run(_config(data_dir), STRATEGY_PATH)
            assert result.meta.total_bars > len(result.trades), (
                f"{result.meta.engine}: total_bars ({result.meta.total_bars}) must not "
                f"degenerate into the trade count ({len(result.trades)})"
            )


def _stub_pyne_cli(captured: dict[str, list[str]], *, trade_csv_text: str = "", stats_csv_text: str = ""):
    """A subprocess.run stand-in for PyneCoreAdapter: records the command and
    writes the CSV outputs the adapter parses, without invoking the CLI."""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        if trade_csv_text:
            Path(cmd[cmd.index("--trade") + 1]).write_text(trade_csv_text)
        if stats_csv_text:
            Path(cmd[cmd.index("--strat") + 1]).write_text(stats_csv_text)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def _run_pyne_with_stubbed_cli(cfg, strategy_path, *, trade_csv_text, stats_csv_text):
    """Run PyneCoreAdapter.run() with the pyne CLI stubbed at the subprocess
    boundary: `shutil.which` resolves a fake binary and `subprocess.run`
    writes the given CSV artifacts and exits cleanly. Returns the result."""
    from contextlib import ExitStack

    def fake_run(cmd, **kwargs):
        trade_csv = Path(cmd[cmd.index("--trade") + 1])
        trade_csv.write_text(trade_csv_text)
        stats_csv = Path(cmd[cmd.index("--strat") + 1])
        stats_csv.write_text(stats_csv_text)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with ExitStack() as stack:
        stack.enter_context(
            patch("runner.engines.pynecore_adapter.shutil.which", return_value="/usr/bin/pyne")
        )
        stack.enter_context(
            patch("runner.engines.pynecore_adapter.subprocess.run", side_effect=fake_run)
        )
        return PyneCoreAdapter().run(cfg, strategy_path)


PYNE_EMPTY_TRADE_CSV = (
    "Trade #,Bar Index,Type,Signal,Date/Time,Price USD,Contracts,"
    "Profit USD,Profit %,Cumulative profit USD,Cumulative profit %,"
    "Run-up USD,Run-up %,Drawdown USD,Drawdown %\n"
)


@pytest.mark.skipif(
    shutil.which("pyne") is None
    and not (Path(sys.executable).parent / "pyne").exists(),
    reason="pyne CLI not installed in this environment",
)
class TestPyneCoreCliEndOfDayBoundary:
    """Conformance (F9): a date-only end bound keeps the whole end day in pyne.

    The adapter hands each symbol's (already F9-filtered) frame to the external
    ``pyne`` CLI and passes ``end_date`` through as ``--to``. The CLI treats a
    date-only ``--to`` as a bound at that day's **midnight** (verified against
    pynesys-pynecore 6.8.14: ``pyne run … --to 2024-01-01`` replays only the
    00:00 bar of the end date, dropping 01:00–23:00) — exactly the
    midnight-truncation the F9 Decision Record removed everywhere else. The
    adapter must therefore translate a date-only end date into the CLI's
    inclusive end-of-day equivalent, so pyne replays the same window as
    native/vectorbt.
    """

    # One round trip that CLOSES inside the end day: entry on the SMA(5/20)
    # crossover at 22:00, exit at the next bar's open 23:00. With the
    # midnight-truncated bound the exit bar is dropped (the position is then
    # force-closed by the CLI at the window edge); with the end-of-day bound
    # the exit lands on the end day like in the other engines.
    PYNE_FIXTURE_PY = '''\
"""@pyne
Contract fixture: SMA crossover entry, next-bar exit (deterministic).
"""

from pynecore import Series
from pynecore.lib import script, strategy, input, ta, plot, color

STRATEGY_META = {
    "name": "Contract Fixture (PyneCore)",
    "version": "1.0.0",
    "tags": {
        "type": ["trend-following"],
        "source": "internal",
        "link": "",
        "asset_class": ["equities"],
        "complexity": "simple",
    },
}


@script.strategy(
    title="Contract Fixture",
    shorttitle="CFX",
    overlay=True,
    default_qty_type=strategy.fixed,
    default_qty_value=1,
    initial_capital=100000,
)
def main(
    src: Series[float] = input.source("close", title="Source"),
    fast_len: int = input.int(5, title="Fast SMA"),
    slow_len: int = input.int(20, title="Slow SMA"),
):
    fast_sma = ta.sma(src, fast_len)
    slow_sma = ta.sma(src, slow_len)

    if ta.crossover(fast_sma, slow_sma):
        strategy.entry("Long", strategy.long)

    if ta.crossunder(fast_sma, slow_sma):
        strategy.close("Long")

    plot(fast_sma, title="Fast SMA", color=color.blue)
    plot(slow_sma, title="Slow SMA", color=color.red)
'''

    @staticmethod
    def _pyne_fixture() -> pd.DataFrame:
        closes = (
            100 + np.sin(np.arange(48) / 3.0) * 8  # SMA(5/20) crossovers within 24 bars
        )
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC"),
                "open": closes,
                "high": closes + 0.3,
                "low": closes - 0.3,
                "close": closes,
                "volume": [1000.0] * 48,
            }
        )

    def _run_pyne_dispatch(self, tmp_path: Path):
        """Write the parity fixture and dispatch it through the adapter with
        the CLI stubbed; returns (strategy_path, captured-command dict)."""
        data = self._pyne_fixture()
        _write_ohlcv(tmp_path, "PARITY", "1h", data)
        strategy_path = tmp_path / "contract_pyne_sma.py"
        strategy_path.write_text(self.PYNE_FIXTURE_PY)

        captured: dict[str, list[str]] = {}
        cfg = _config(
            tmp_path,
            strategy_name="contract_pyne_sma",
            start_date="2024-01-01",
            end_date="2024-01-01",
            parameters={},
        )
        with patch(
            "runner.engines.pynecore_adapter.subprocess.run",
            side_effect=_stub_pyne_cli(captured, stats_csv_text="Metric,All USD\n"),
        ):
            PyneCoreAdapter().run(cfg, str(strategy_path))
        return strategy_path, captured

    @staticmethod
    def _run_pyne(
        tmp_path: Path, strategy_path: Path, csv_path: Path, end_bound: str, start_bound: str = "2024-01-01"
    ) -> Path:
        workdir = tmp_path / "workdir"
        workdir.mkdir(exist_ok=True)
        trade_csv = tmp_path / "trades.csv"
        stats_csv = tmp_path / "stats.csv"
        pyne_bin = shutil.which("pyne") or str(Path(sys.executable).parent / "pyne")
        cmd = [
            pyne_bin,
            "--workdir",
            str(workdir),
            "run",
            str(strategy_path),
            str(csv_path),
            "--from",
            start_bound,
            "--to",
            end_bound,
            "--trade",
            str(trade_csv),
            "--strat",
            str(stats_csv),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            pytest.fail(f"pyne CLI failed: {result.stderr[-500:]}")
        return trade_csv

    def test_pyne_replays_the_full_end_day(self, tmp_path: Path):
        """The exit on the end day must survive a date-only end date.

        With the naive date-only ``--to`` (the pre-fix behavior) the CLI cuts
        at the end date's midnight: the exit bar (23:00) is never replayed and
        the trade closes one bar late/at the window edge instead.
        """
        data = self._pyne_fixture()
        _write_ohlcv(tmp_path, "PARITY", "1h", data)
        csv_path = tmp_path / "parity_1h.csv"
        data[["timestamp", "open", "high", "low", "close", "volume"]].to_csv(
            csv_path, index=False
        )
        strategy_path = tmp_path / "contract_pyne_sma.py"
        strategy_path.write_text(self.PYNE_FIXTURE_PY)

        # The adapter's end bound for end_date="2024-01-01" must be the
        # end-of-day instant, not the raw date string.
        assert _pyne_cli_end_bound("2024-01-01") == "2024-01-01T23:59:59.999999+00:00"

        trade_csv = self._run_pyne(
            tmp_path, strategy_path, csv_path, _pyne_cli_end_bound("2024-01-01")
        )
        trades = _parse_pyne_trade_csv(trade_csv, "PARITY", 0)
        assert trades, "fixture must produce at least one trade"
        exit_time = pd.Timestamp(trades[-1].exits[-1].time)
        assert exit_time <= pd.Timestamp("2024-01-01 23:59:59.999999", tz="UTC"), (
            "the end day's exit bar was replayed, so the trade must close on the end day"
        )

    def test_date_only_end_bound_translates_to_end_of_day(self):
        assert _pyne_cli_end_bound("2024-01-01") == "2024-01-01T23:59:59.999999+00:00"
        # A bound that already carries a time component is used as-is (F9).
        assert _pyne_cli_end_bound("2024-01-01T14:30:00Z") == "2024-01-01T14:30:00Z"

    def test_adapter_passes_translated_bound_to_the_cli(self, tmp_path: Path):
        """The adapter must not pass the raw date-only string as ``--to``.

        The adapter's own parquet filter keeps the full end day (F9); if the
        CLI then cuts at midnight, the pyne backtest window is a strict subset
        of the native/vectorbt window for the same configuration.
        """
        _strategy_path, captured = self._run_pyne_dispatch(tmp_path)

        cmd = captured["cmd"]
        end_bound = cmd[cmd.index("--to") + 1]
        assert end_bound == _pyne_cli_end_bound("2024-01-01"), (
            "the adapter must translate a date-only end date into the CLI's "
            "end-of-day bound, otherwise pyne replays a truncated window"
        )

    def test_adapter_passes_utc_start_bound_to_the_cli(self, tmp_path: Path):
        """A date-only start date must reach the CLI as an AWARE UTC instant.

        The CLI parses ``--from`` with ``datetime.fromisoformat`` and resolves
        the resulting NAIVE datetime through ``timestamp()`` — i.e. in the HOST
        timezone. Under ``TZ=America/New_York`` a date-only ``--from
        2024-01-01`` lands on 05:00Z and drops the first five UTC bars of the
        day; the replay window then depends on where the adapter happens to
        run. F9 fixes the convention: naive timestamps are UTC, so the adapter
        must pass an aware UTC start-of-day instant (both bounds aware = host
        TZ irrelevant).
        """
        _strategy_path, captured = self._run_pyne_dispatch(tmp_path)

        cmd = captured["cmd"]
        start_bound = cmd[cmd.index("--from") + 1]
        assert start_bound == _pyne_cli_start_bound("2024-01-01"), (
            "the adapter must translate a date-only start date into an aware "
            "UTC start-of-day instant, otherwise the replay window depends on "
            "the host timezone"
        )
        # The instant must carry an explicit offset so the CLI's naive-parse
        # host-TZ fallback can never apply.
        assert "+" in start_bound or "Z" in start_bound, (
            f"start bound {start_bound!r} must be timezone-aware"
        )

    def test_pyne_replays_the_same_window_in_a_western_tz(self, tmp_path: Path):
        """Real-CLI TZ pin: the replay window must not move with the host TZ.

        Runs the same fixture the UTC test runs, but with the process timezone
        set to America/New_York. Pre-fix, the naive date-only ``--from``
        shifted the window start to 05:00Z and the round trip inside the end
        day vanished (0 trades instead of 2).
        """
        data = self._pyne_fixture()
        csv_path = tmp_path / "parity_1h.csv"
        data[["timestamp", "open", "high", "low", "close", "volume"]].to_csv(
            csv_path, index=False
        )
        strategy_path = tmp_path / "contract_pyne_sma.py"
        strategy_path.write_text(self.PYNE_FIXTURE_PY)

        self._run_with_host_tz(
            "America/New_York",
            lambda: self._assert_round_trip_survives(tmp_path, strategy_path, csv_path),
        )

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _run_with_host_tz(tz_name: str, fn) -> None:
        """Run ``fn`` with the process TZ set to ``tz_name`` (restored after)."""
        import os
        import time

        old_tz = os.environ.get("TZ")
        old_tzset = getattr(time, "tzset", None)
        os.environ["TZ"] = tz_name
        if old_tzset is not None:
            old_tzset()
        try:
            fn()
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            if old_tzset is not None:
                old_tzset()

    def _assert_round_trip_survives(
        self, tmp_path: Path, strategy_path: Path, csv_path: Path
    ) -> None:
        from datetime import timezone

        end_instant = (
            pd.Timestamp(_pyne_cli_end_bound("2024-01-01"))
            .to_pydatetime()
            .astimezone(timezone.utc)
        )
        trade_csv = self._run_pyne(
            tmp_path,
            strategy_path,
            csv_path,
            _pyne_cli_end_bound("2024-01-01"),
            start_bound=_pyne_cli_start_bound("2024-01-01"),
        )
        trades = _parse_pyne_trade_csv(trade_csv, "PARITY", 0)
        assert trades, (
            "the round trip inside the end day vanished — the replay window "
            "moved with the host timezone (naive --from host-TZ resolution)"
        )
        last_exit = pd.Timestamp(trades[-1].exits[-1].time)
        assert last_exit <= pd.Timestamp(end_instant), (
            f"the last exit {last_exit} must land on the end day "
            f"(<= {end_instant.isoformat()})"
        )


def _read_parquet(data_dir: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    path = data_dir / "ohlcv" / "test_source" / "equities" / symbol / f"{timeframe}.parquet"
    return pd.read_parquet(path)


# ─── Fills (F1/F2) ───────────────────────────────────────────────────────────


class TestFillContract:
    """Conformance: market entries fill at the next bar's open.

    Native and vectorbt share this convention for market entries; the
    signal-replay engine fills same-bar at the signal bar's close (documented
    exception — see the contract document's execution-timing section).
    """

    def test_native_engine_fills_market_entry_at_next_bar_open(self):
        data = _deterministic_ohlcv(periods=30, freq="h")

        from runner.engine import TradeEngine

        def strategy_fn(df: pd.DataFrame, params: dict):
            entries = pd.Series(False, index=df.index)
            exits = pd.Series(False, index=df.index)
            entries.iloc[2] = True
            exits.iloc[10] = True
            return entries, exits, {}, []

        cfg = BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="unit-test",
            symbols=["PARITY"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000,
            modeling_resolution="main_timeframe",
            parameters={"fees": "zero"},
        )
        result = TradeEngine(cfg, symbol="PARITY").run(data, strategy_fn)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(float(data.iloc[3]["open"]))
        assert trade.entry_time == pd.Timestamp(data.iloc[3]["timestamp"]).isoformat()

    def test_vectorbt_engine_fills_market_entry_at_next_bar_open(self, tmp_path: Path):
        data = _deterministic_ohlcv()  # 260 bars: enough for two round trips
        _write_ohlcv(tmp_path, "PARITY", "1h", data)

        cfg = _config(tmp_path, parameters={"fast_length": 5, "slow_length": 20})
        result = VectorbtAdapter().run(cfg, STRATEGY_PATH)

        assert len(result.trades) >= 1
        for trade in result.trades:
            entry_ts = pd.Timestamp(trade.entry_time)
            bar = data.loc[data["timestamp"] == entry_ts].iloc[0]
            assert trade.entry_price == pytest.approx(float(bar["open"])), (
                "vectorbt market entries must fill at the (shifted) bar's open"
            )

    def test_native_and_vectorbt_agree_on_entry_fills_for_the_bundled_fixture(
        self, contract_data_dir: Path
    ):
        data_dir = tmp_contract_dir(contract_data_dir)
        cfg = _config(data_dir)

        native = NativeAdapter().run(cfg, STRATEGY_PATH)
        vectorbt = VectorbtAdapter().run(cfg, STRATEGY_PATH)

        assert len(native.trades) == len(vectorbt.trades)
        # `len(...) == len(...)` is asserted on the line above, so the strict zip
        # is the loop's own restatement of that same invariant.
        for native_trade, vectorbt_trade in zip(native.trades, vectorbt.trades, strict=True):
            assert pd.Timestamp(native_trade.entry_time) == pd.Timestamp(vectorbt_trade.entry_time)
            assert native_trade.entry_price == pytest.approx(vectorbt_trade.entry_price, rel=1e-9)

    def test_signal_replay_engine_fills_at_signal_bar_close(self):
        """Documented exception (F10): signal-replay fills same-bar at close."""
        frame = _deterministic_ohlcv(periods=40, freq="h")
        signal = ReplaySignal.from_payload(
            {
                "timestamp": "2024-01-01T05:00:00Z",
                "symbol": "PARITY",
                "action": "buy",
                "entry_price": float(frame.iloc[5]["close"]),
            }
        )
        result = SignalReplayAdapter(fee_params={"fees": "zero"}, ).run(
            signals=[signal],
            ohlcv_by_symbol={"PARITY": frame},
            timeframe="1h",
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        # Same-bar close fill, at the bar whose timestamp carries the signal.
        assert trade.entry_time == pd.Timestamp(frame.iloc[5]["timestamp"]).isoformat()
        assert trade.entry_price == pytest.approx(float(frame.iloc[5]["close"]))


# ─── Pynecore restatement containment (F10) ──────────────────────────────────


class TestPynecoreRestatementContainment:
    """Conformance (F10): the post-hoc restatement may only tighten exits.

    ``_normalize_trade_exits`` rewrites an exit leg's price/reason when the
    recorded exit price violates the recorded protective level. The restated
    price must always be the level itself — never a level beyond it — and the
    reason must be the matching protective reason.
    """

    def _trade(self, direction: str, exit_price: float, meta: dict) -> Trade:
        return Trade(
            id=1,
            symbol="PARITY",
            direction=direction,
            entry_time="2024-01-01T00:00:00+00:00",
            entry_price=100.0,
            quantity=1.0,
            exits=[
                TradeExit(
                    time="2024-01-01T05:00:00+00:00",
                    price=exit_price,
                    quantity=1.0,
                    pnl=0.0,
                    reason="signal",
                )
            ],
            pnl=0.0,
            meta=meta,
        )

    def test_stop_violating_exit_is_restated_to_the_stop(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 94.0, {"sl_price": 95.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(95.0)
        assert trades[0].exits[-1].reason == "stop_loss"

    def test_tp_violating_exit_is_restated_to_the_take_profit(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 121.0, {"tp_price": 120.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(120.0)
        assert trades[0].exits[-1].reason == "take_profit"

    def test_exit_within_levels_is_left_alone(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 105.0, {"sl_price": 95.0, "tp_price": 120.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(105.0)
        assert trades[0].exits[-1].reason == "signal"

    def test_short_side_restatement_is_mirrored(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("SHORT", 126.0, {"sl_price": 125.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price == pytest.approx(125.0)
        assert trades[0].exits[-1].reason == "stop_loss"

    def test_restated_price_is_never_pushed_beyond_the_level(self):
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        trades = [self._trade("LONG", 90.0, {"sl_price": 95.0})]
        _normalize_trade_exits(trades)
        assert trades[0].exits[-1].price >= 95.0 - 1e-9

    def test_instrumented_restatement_only_touches_violating_legs(self):
        """Property: the restatement is a containment, not a rewrite.

        For any exit price at/inside the levels, the leg is untouched; for any
        violating price, the restated price is exactly the level.
        """
        from runner.engines.pynecore_adapter import _normalize_trade_exits

        for direction in ("LONG", "SHORT"):
            long = direction == "LONG"
            sl = 95.0 if long else 125.0
            tp = 120.0 if long else 105.0
            inside = [sl + 1, (sl + tp) / 2, tp - 1] if long else [sl - 1, (sl + tp) / 2, tp + 1]
            for price in inside:
                trades = [self._trade(direction, price, {"sl_price": sl, "tp_price": tp})]
                _normalize_trade_exits(trades)
                assert trades[0].exits[-1].price == pytest.approx(price), (
                    f"{direction} exit {price} inside levels must not be restated"
                )
            for violating in ([90.0, 80.0] if long else [130.0, 140.0]):
                trades = [self._trade(direction, violating, {"sl_price": sl, "tp_price": tp})]
                _normalize_trade_exits(trades)
                assert trades[0].exits[-1].price == pytest.approx(sl)
                assert trades[0].exits[-1].reason == "stop_loss"


def test_normalize_ohlcv_frame_fills_missing_and_all_nan_from_bid():
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC"),
            "high": [np.nan] * 4,  # all-NaN canonical (index feeds)
            "low": [1.0, 1.1, 1.2, 1.3],  # healthy canonical wins
            "bid_high": [2.0, 2.1, 2.2, 2.3],
            "bid_low": [9.0, 9.1, 9.2, 9.3],
            "bid_close": [3.0, np.nan, 3.2, 3.3],  # partial NaN stays NaN (no close col)
            "ask_high": [2.5, 2.6, 2.7, 2.8],  # ask_* must survive
        }
    )
    out = normalize_ohlcv_frame(frame)
    assert out["high"].tolist() == [2.0, 2.1, 2.2, 2.3]
    assert out["low"].tolist() == [1.0, 1.1, 1.2, 1.3]
    assert "open" not in out.columns  # no bid_open -> not fabricated
    assert out["ask_high"].tolist() == [2.5, 2.6, 2.7, 2.8]


def test_normalize_ohlcv_frame_does_not_mutate_input():
    # C-1: the helper works on a copy; callers must use the return value.
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
            "high": [np.nan, np.nan],
            "bid_high": [2.0, 2.1],
        }
    )
    snapshot = frame.copy(deep=True)
    out = normalize_ohlcv_frame(frame)
    pd.testing.assert_frame_equal(frame, snapshot)
    assert out["high"].tolist() == [2.0, 2.1]


def test_intrabar_resolution_equal_to_base_resolves_to_none():
    """F8 boundary: an intrabar frame must be strictly FINER than the main
    timeframe — requesting the same granularity ("1h" intrabar on "1h"
    data) resolves to no intrabar walk, never to a self-referential window."""
    assert resolve_intrabar_timeframe("1h", "1h") is None
    assert resolve_intrabar_timeframe("1d", "1d") is None
    assert resolve_intrabar_timeframe("1h", "1m") == "1m"
    assert resolve_intrabar_timeframe("1h", "4h") is None
